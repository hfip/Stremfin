from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def auth():
    client.post('/api/login', json={'username': 'admin', 'password': 'admin'})


def test_manifest_catalog_discovery_and_selection(monkeypatch):
    auth()
    async def manifests(self):
        return [{'addon_url':'https://addon.test','type':'movie','id':'popular','name':'Popular Movies'}]
    monkeypatch.setattr('app.main.MetadataService.manifests', manifests)
    response = client.get('/api/addons/catalogs')
    assert response.status_code == 200 and response.json()['catalogs'][0]['id'] == 'popular'
    saved = client.put('/api/addons/catalogs', json=response.json()['catalogs'])
    assert saved.status_code == 200


def test_live_series_hierarchy(monkeypatch):
    auth()
    async def details(self, item_id, kind, addon_urls):
        return {'id':item_id,'name':'Live Series','type':'Series','overview':'','videos':[{'id':'tt-live:s1e1','name':'Pilot','season':1,'episode':1},{'id':'tt-live:s1e2','name':'Second','season':1,'episode':2}]}
    monkeypatch.setattr('app.api.jellyfin.MetadataService.details', details)
    seasons = client.get('/Shows/tt-live/Seasons')
    episodes = client.get('/Shows/tt-live/Episodes', params={'Season':1})
    item = client.get('/Items/tt-live')
    assert seasons.json()['Items'][0]['IndexNumber'] == 1
    assert len(episodes.json()['Items']) == 2
    assert item.json()['Name'] == 'Live Series'
