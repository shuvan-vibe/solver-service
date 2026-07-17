import urllib.request
from urllib.error import HTTPError

try:
    req = urllib.request.Request('https://solver-service-production-fcee.up.railway.app/getToken', method='GET')
    res = urllib.request.urlopen(req)
    print("SUCCESS:\n", res.read().decode())
except HTTPError as e:
    print(f"HTTP ERROR {e.code}:\n", e.read().decode())
except Exception as e:
    print("OTHER ERROR:\n", e)
