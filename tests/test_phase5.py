from fastapi.testclient import TestClient
from app.main import app
from app.services.metadata import MetadataService

client = TestClient(app)


def test_user_views_route():
    response = client.get('/UserViews')
    assert response.status_code == 200
    assert {x['CollectionType'] for x in response.json()['Items']} == {'movies', 'tvshows'}


def test_manifest_url_normalization(monkeypatch):
    seen = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'catalogs':[{'type':'movie','id':'top','name':'Top'}]}
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def get(self, url): seen.append(url); return Response()
    monkeypatch.setattr('app.services.metadata.httpx.AsyncClient', lambda **kwargs: Client())
    service = MetadataService(type('Settings', (), {'addon_urls':['https://addon.test/manifest.json'], 'request_timeout_seconds':1})())
    result = __import__('asyncio').run(service.manifests())
    assert seen == ['https://addon.test/manifest.json']
    assert result[0]['addon_url'] == 'https://addon.test'


def test_preferences_are_lists():
    client.post('/api/login', json={'username':'admin','password':'admin'})
    payload = client.get('/api/settings').json()
    assert isinstance(payload['preferred_resolutions'], list)
    assert isinstance(payload['preferred_audio_formats'], list)
