"""
Crypto Arbitrage Detector - Get Quote Pair Module (hardened)
- 并发控制：全局/每主机信号量
- 重试与指数退避：针对 429/5xx/网络错误
- 超时：统一超时
- 代理：可选轮询代理（没有也能跑）
- 健壮化：字段缺失兜底、类型转换更稳
"""
import math
import sys, os
import asyncio
import aiohttp
import urllib.parse
from typing import List, Dict, Callable, Optional

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from crypto_arbitrage_detector.utils.data_structures import TokenInfo, EdgePairs
from crypto_arbitrage_detector.configs.request_config import (
    jupiter_quote_api, jupiter_swap_api, solana_rpc_api
)
from crypto_arbitrage_detector.utils.enrich_gas_fee import enrich_responses_with_gas_fee

# ===== 新增：可调参数 =====
CONCURRENCY_GLOBAL = 20              # 全局并发上限（根据你的机器/配额调整）
CONCURRENCY_PER_HOST = 6             # 每主机并发上限，避免打爆同一域名
REQUEST_TIMEOUT_S = 8                # 单次请求总超时
MAX_RETRIES = 3                      # 重试次数（含首次）
BACKOFF_BASE_S = 0.25                # 指数退避基数（0.25 -> 0.5 -> 1.0 ...）
RETRY_STATUSES = {429, 500, 502, 503, 504}

# ===== 新增：可选 proxy 轮询（没有代理也可以不传）=====
class RoundRobinProxy:
    def __init__(self, proxies: Optional[List[str]] = None):
        self.proxies = (proxies or []).copy()
        self.idx = 0
        self._lock = asyncio.Lock()

    async def get(self, host: str) -> Optional[str]:
        # host 预留参数：未来可做 per-host 选择策略
        if not self.proxies:
            return None
        async with self._lock:
            url = self.proxies[self.idx % len(self.proxies)]
            self.idx += 1
            return url

# ===== 新增：每主机信号量（简易限流）=====
class HostSemaphores:
    def __init__(self, per_host: int):
        self.per_host = per_host
        self._sems: Dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, host: str):
        async with self._lock:
            if host not in self._sems:
                self._sems[host] = asyncio.Semaphore(self.per_host)
            sem = self._sems[host]
        await sem.acquire()
        return sem  # 归还时 sem.release()

host_sems = HostSemaphores(CONCURRENCY_PER_HOST)
global_sem = asyncio.Semaphore(CONCURRENCY_GLOBAL)

# ===== 工具：带重试/退避的 GET =====
async def get_json_with_resilience(
    session: aiohttp.ClientSession,
    url: str,
    params: Dict,
    headers: Dict,
    proxy_getter: Optional[RoundRobinProxy] = None,
) -> Dict:
    host = urllib.parse.urlparse(url).hostname or "default"
    backoff = BACKOFF_BASE_S

    for attempt in range(1, MAX_RETRIES + 1):
        # 并发控制：全局 + 每主机
        async with global_sem:
            sem = await host_sems.acquire(host)
            try:
                proxy = await (proxy_getter.get(host) if proxy_getter else None)
                timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
                async with session.get(url, params=params, headers=headers, proxy=proxy, timeout=timeout) as resp:
                    status = resp.status
                    if status == 200:
                        # 尽量用 resp.json(loads=...) 以防大数；这里保持默认
                        return await resp.json()
                    if status in RETRY_STATUSES:
                        # 可重试状态：退避后再试
                        await asyncio.sleep(backoff)
                        backoff = min(2.0, backoff * 2)
                        continue
                    # 不可重试，直接返回空
                    # 你也可以 raise 让上层决定
                    return {}
            except (aiohttp.ClientError, asyncio.TimeoutError):
                # 网络错误/超时：退避重试
                await asyncio.sleep(backoff)
                backoff = min(2.0, backoff * 2)
                continue
            finally:
                sem.release()
    return {}

# ===== 原 fetch_quote 改造：调用上面的弹性 GET =====
async def fetch_quote(
        session: aiohttp.ClientSession,
        input_mint: str,
        output_mint: str,
        amount: int = jupiter_quote_api["default_tx_amount"],
        quote_url: str = jupiter_quote_api["base_url"],
        api_key: str = jupiter_quote_api["api_key"],
        from_symbol=None,
        to_symbol=None,
        proxy_getter: Optional[RoundRobinProxy] = None,
        extra_headers: Optional[Dict] = None,
        # ssl: bool = True,  # 如遇证书问题，可暴露出来
        ) -> Dict:
    """
    Fetch quote from Jupiter API for a given token pair with retries/timeout/proxy.
    """
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount,
        # "slippageBps": jupiter_quote_api["default_slippage_bps"]
    }

    headers = jupiter_quote_api["headers"].copy()
    if api_key:
        headers["x-api-key"] = api_key
    headers.setdefault("Content-Type", "application/json")
    if extra_headers:
        headers.update(extra_headers)

    data = await get_json_with_resilience(
        session=session,
        url=quote_url,
        params=params,
        headers=headers,
        proxy_getter=proxy_getter,
    )
    if data:
        data["from_symbol"] = from_symbol
        data["to_symbol"] = to_symbol
    return data

# ===== 主流程：批量请求 + 富化 gas + 构造 EdgePairs =====
async def get_edge_pairs(
        token_list: List[TokenInfo],
        tx_amount: int = jupiter_quote_api["default_tx_amount"],
        api_key: str = jupiter_quote_api["api_key"],
        quote_url: str = jupiter_quote_api["base_url"],
        swap_url: str = jupiter_swap_api["base_url"],
        solana_rpc: str = solana_rpc_api["base_url"],
        proxies: Optional[List[str]] = None,   # 新增：可选代理列表
        ) -> List[EdgePairs]:
    """
    Fetch edge pairs from Jupiter API for all token combinations.
    """
    edge_pairs: List[EdgePairs] = []
    proxy_getter = RoundRobinProxy(proxies)

    connector = aiohttp.TCPConnector(limit=CONCURRENCY_GLOBAL)  # 连接池上限
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = []
        for token_in in token_list:
            for token_out in token_list:
                if token_in.address == token_out.address:
                    continue
                tasks.append(
                    fetch_quote(
                        session,
                        token_in.address,
                        token_out.address,
                        tx_amount,
                        quote_url=quote_url,
                        api_key=api_key,
                        from_symbol=token_in.symbol,
                        to_symbol=token_out.symbol,
                        proxy_getter=proxy_getter,
                    )
                )

        # 并发执行（保留顺序无所谓时，用 as_completed 更省内存）
        responses = await asyncio.gather(*tasks, return_exceptions=False)

    # 过滤最小可用返回
    responses = [
        r for r in responses
        if isinstance(r, dict) and r.get("inputMint") and r.get("routePlan") is not None and r.get("outAmount") and r.get("inAmount")
    ]

    # 富化 gas 费用
    responses = await enrich_responses_with_gas_fee(responses, api_key, swap_url, solana_rpc)

    # 价格映射：mint -> price_in_SOL
    price_map = generate_price_map_from_responses(responses)

    # 组装 EdgePairs（健壮化处理）
    for data in responses:
        try:
            out_amount = float(data.get("outAmount", 0) or 0)
            in_amount = float(data.get("inAmount", 0) or 0)
            if in_amount <= 0 or out_amount <= 0:
                continue

            total_fee_sol = 0.0
            for route in (data.get("routePlan") or []):
                if not route:
                    continue
                swap_info = route.get("swapInfo") or {}
                fee_str = swap_info.get("feeAmount")
                fee_mint = swap_info.get("feeMint")
                if fee_str and fee_mint:
                    try:
                        fee = float(fee_str)
                        price_in_sol = float(price_map.get(fee_mint, 0.0))
                        total_fee_sol += fee * price_in_sol
                    except (ValueError, TypeError):
                        pass

            platform_fee_info = data.get("platformFee")
            if isinstance(platform_fee_info, dict):
                try:
                    platform_fee = float(platform_fee_info.get("amount", 0) or 0)
                except (ValueError, TypeError):
                    platform_fee = 0.0
            else:
                platform_fee = 0.0

            out_mint = data["outputMint"]
            in_mint = data["inputMint"]

            out_amount_in_sol = out_amount * float(price_map.get(out_mint, 0.0))
            in_amount_in_sol  = in_amount  * float(price_map.get(in_mint,  0.0))
            if in_amount_in_sol <= 0:
                continue

            price_ratio = out_amount_in_sol / in_amount_in_sol
            # 避免 log(<=0)
            if price_ratio <= 0:
                continue
            weight = -math.log(price_ratio)

            gas_fee = float(data.get("gasFee", 0) or 0)  # lamports；按需外部再转 SOL

            edge = EdgePairs(
                from_token=in_mint,
                to_token=out_mint,
                from_symbol=data.get("from_symbol"),
                to_symbol=data.get("to_symbol"),
                in_amount=in_amount_in_sol,
                out_amount=out_amount_in_sol,
                price_ratio=price_ratio,
                weight=weight,
                slippage_bps=int(data.get("slippageBps", 0) or 0),
                platform_fee=platform_fee,
                price_impact_pct=float(data.get("priceImpactPct", 0.0) or 0.0),
                total_fee=total_fee_sol,
                gas_fee=gas_fee,
            )
            edge_pairs.append(edge)
        except Exception as e:
            # 保持静默/或改成 logger.warning
            print(f"Error processing response: {e}")
            continue

    return edge_pairs

# Helper: 生成 mint 对 SOL 的价格映射
def generate_price_map_from_responses(responses: List[Dict]) -> Dict[str, float]:
    """
    从响应中抽取 mint->price_in_SOL 的近似映射（使用 in/out 原子单位比值，足够用于相对比较）
    """
    sol = jupiter_quote_api['sol_mint']
    price_map: Dict[str, float] = {sol: 1.0}

    for data in responses:
        try:
            in_mint = data["inputMint"]
            out_mint = data["outputMint"]
            in_amt = float(data["inAmount"])
            out_amt = float(data["outAmount"])
            if in_amt <= 0 or out_amt <= 0:
                continue

            if in_mint == sol:
                # 1 SOL = ? out_token  => out_token/SOL；求反得 SOL/out_token
                price_map[out_mint] = 1.0 / (out_amt / in_amt)
            elif out_mint == sol:
                # 1 in_token = ? SOL
                price_map[in_mint] = out_amt / in_amt
        except (KeyError, ZeroDivisionError, TypeError, ValueError):
            continue
    return price_map
