"""Exercise the public MCP route and its real session lifecycle."""
import json

from starlette.testclient import TestClient

from mcp_server.server import Settings, create_app


def _message(response):
    response.raise_for_status()
    if response.headers['content-type'].startswith('application/json'):
        return response.json()
    return json.loads(next(line[6:] for line in response.text.splitlines()
                           if line.startswith('data: ')))


def test_http_initialize_list_and_resolve(deps):
    app = create_app(Settings(), deps)
    headers = {'Accept': 'application/json, text/event-stream'}
    with TestClient(app) as client:
        response = client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                       'clientInfo': {'name': 'http-regression', 'version': '1.0'}},
        })
        assert 'result' in _message(response)
        headers['Mcp-Session-Id'] = response.headers['mcp-session-id']
        response = client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        assert response.status_code == 202
        listed = _message(client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}))
        assert 'resolve-library-id' in {t['name'] for t in listed['result']['tools']}
        resolved = _message(client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
            'params': {'name': 'resolve-library-id', 'arguments': {'libraryName': 'fastapi'}},
        }))
        result = resolved['result']
        assert not result.get('isError', False)
        payload = json.loads(result['content'][0]['text'])
        assert payload['library_id'] == 'fastapi'
        assert client.post('/mcp', headers={**headers, 'Origin': 'https://untrusted.invalid'},
                           json={'jsonrpc': '2.0', 'id': 4, 'method': 'tools/list'}).status_code == 403
