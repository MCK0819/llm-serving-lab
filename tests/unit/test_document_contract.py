from app.core.settings import Settings
from app.main import create_app


def test_openapi_describes_one_binary_pdf_and_authentication():
    schema = create_app(Settings()).openapi()
    operation = schema["paths"]["/documents"]["post"]
    body = operation["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert body["required"] == ["file"]
    assert body["properties"]["file"] == {"type": "string", "format": "binary"}
    assert operation["security"] == [{"HTTPBearer": []}]
