from aiohttp import ClientSession, ClientTimeout

from chia.farmer.pooling.og_pool_protocol import SubmitPartial

timeout = ClientTimeout(total=30)


class PoolApiClient:
    base_url: str

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    async def get_pool_info(self):
        async with ClientSession(timeout=timeout) as client:
            async with client.get(f"{self.base_url}/pool_info") as res:
                return await res.json()

    async def submit_partial(self, submit_partial: SubmitPartial):
        async with ClientSession(timeout=timeout) as client:
            async with client.post(f"{self.base_url}/partial", json=submit_partial.to_json_dict()) as res:
                return await res.json()
