from app.core.settings import Settings
from app.main import create_app


async def test_app_owns_http_client_and_closes_it_at_shutdown() -> None:
    app = create_app(Settings(inference_url="http://127.0.0.1:1"))
    async with app.router.lifespan_context(app):
        client = app.state.inference.http
        assert not client.is_closed
        assert app.state.inference.http is client
    assert client.is_closed


async def test_apps_do_not_share_connection_pools() -> None:
    first = create_app()
    second = create_app()
    async with first.router.lifespan_context(first):
        async with second.router.lifespan_context(second):
            assert first.state.inference.http is not second.state.inference.http
        assert not first.state.inference.http.is_closed
