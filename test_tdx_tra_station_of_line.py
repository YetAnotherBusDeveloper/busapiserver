"""Test TDX TRA StationOfLine API directly.

Manual probe script (not pytest) -- same pattern as test_tdx_metro.py.
Answers: nested-vs-flat shape, exact key names, whether Direction exists for TRA.
"""
from dotenv import load_dotenv
import os
import requests
import json

load_dotenv()

auth_url = 'https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token'
auth_data = {
    'grant_type': 'client_credentials',
    'client_id': os.getenv('TDX_CLIENT_ID'),
    'client_secret': os.getenv('TDX_CLIENT_SECRET'),
}
token_resp = requests.post(auth_url, data=auth_data)
token = token_resp.json().get('access_token')
headers = {'Authorization': f'Bearer {token}', 'Accept-Encoding': 'gzip'}

print('=' * 60)
print('=== TRA StationOfLine ===')
print('=' * 60)
url = 'https://tdx.transportdata.tw/api/basic/v2/Rail/TRA/StationOfLine'
resp = requests.get(url, headers=headers, params={'$top': 3, '$format': 'JSON'})
print(f'Status: {resp.status_code}')
data = resp.json()
print(f'Type of payload: {type(data).__name__}')
if isinstance(data, dict):
    print(f'Dict keys: {list(data.keys())}')
    data = data.get('StationOfLines') or data.get('Items') or []
print(f'Row count (with $top=3): {len(data)}')
if data:
    print('--- item[0] full JSON ---')
    print(json.dumps(data[0], ensure_ascii=False, indent=2)[:3000])
    print('--- item[0] top-level keys ---')
    print(list(data[0].keys()))
    nested = data[0].get('Stations')
    if nested:
        print(f'NESTED shape. Station count in line 0: {len(nested)}')
        print('Nested station keys:', list(nested[0].keys()))
    else:
        print('FLAT shape (no Stations array on the item)')

print()
print('=' * 60)
print('=== Full fetch: how many rows total, and line ids ===')
print('=' * 60)
resp2 = requests.get(url, headers=headers, params={'$format': 'JSON'})
data2 = resp2.json()
if isinstance(data2, dict):
    data2 = data2.get('StationOfLines') or data2.get('Items') or []
print(f'Total rows: {len(data2)}')
for item in data2:
    line_id = item.get('LineID', '?')
    line_name = (item.get('LineName') or {}).get('Zh_tw', '') if isinstance(item.get('LineName'), dict) else item.get('LineName', '')
    stations = item.get('Stations') or []
    print(f'  LineID={line_id!r:10} LineName={line_name!r:12} Direction={item.get("Direction")!r:6} stations={len(stations)}')

print()
print('=' * 60)
print('=== TRA Line (for line names) ===')
print('=' * 60)
resp3 = requests.get('https://tdx.transportdata.tw/api/basic/v2/Rail/TRA/Line', headers=headers, params={'$format': 'JSON'})
data3 = resp3.json()
if isinstance(data3, dict):
    data3 = data3.get('Lines') or data3.get('Items') or []
print(f'Total lines: {len(data3)}')
for item in data3:
    print(f'  {item.get("LineID")!r:10} {(item.get("LineName") or {}).get("Zh_tw","")!r:14} LineNo={item.get("LineNo")!r}')
