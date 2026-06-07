import asyncio
import json
import websockets
import sys

async def test_focus(ip):
    uri = f"ws://localhost:8000/ws?focus={ip}"
    try:
        async with websockets.connect(uri) as websocket:
            message = await websocket.recv()
            data = json.loads(message)
            f_data = data.get('focused_data')
            if f_data:
                # Find a Healthy Gigabit row
                healthy = [row for row in f_data if 'Gi' in row.get('ifDescr', '') and row.get('ifHighSpeed') == 1000]
                if healthy:
                    print(f"HEALTHY Gi PORT ON {ip}:")
                    print(json.dumps(healthy[0], indent=2))
                
                # Find a Degraded Gigabit row
                degraded = [row for row in f_data if 'Gi' in row.get('ifDescr', '') and row.get('is_degraded') == True]
                if degraded:
                    print(f"DEGRADED Gi PORT ON {ip}:")
                    print(json.dumps(degraded[0], indent=2))
                return True
    except Exception as e:
        print(f"Error: {e}")
    return False

if __name__ == "__main__":
    ip_to_test = sys.argv[1] if len(sys.argv) > 1 else "10.160.4.1"
    asyncio.run(test_focus(ip_to_test))
