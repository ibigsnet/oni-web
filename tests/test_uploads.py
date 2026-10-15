from test_gateway import gateway, http, run


def test_browser_uploads_accept_bytes_and_reject_backend_paths(tmp_path):
    async def scenario():
        async with gateway(tmp_path) as (_, base):
            headers = {"X-ONI-Client": "tab-one", "X-ONI-Filename": "..%2Fvault.key"}
            assert (await http(base, "/api/uploads", "POST", {}, headers=headers))[0] == 400
            headers["X-ONI-Filename"] = "map.json"
            status, value, _ = await http(base, "/api/uploads", "POST", {}, headers=headers)
            assert status == 200 and value == {"name": "map.json", "size": 2}
            assert (await http(base, "/api/uploads", "POST", {}, headers=headers))[0] == 400
            assert (await http(base, "/api/rpc/upload_clear", "POST", {}, headers=headers))[0] == 200
            assert (await http(base, "/api/uploads", "POST", {}, headers=headers))[0] == 200
    run(scenario())
