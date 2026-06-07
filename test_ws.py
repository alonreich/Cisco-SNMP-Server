import asyncio
import json
import websockets
import sys

async def test_focus(ip):
    uri = f"ws://localhost:8000/ws?focus={ip}"
    print(f"Connecting to {uri}...")
    try:
        async with websockets.connect(uri) as websocket:
            print("Connected. Waiting for initial burst...")
            message = await websocket.recv()
            data = json.loads(message)
            
            print(f"Received type: {data.get('type')}")
            print(f"Focused IP in msg: {data.get('focused_ip')}")
            f_data = data.get('focused_data')
            if f_data is not None:
                print(f"Focused data received! Count: {len(f_data)} interfaces")
                if len(f_data) > 0:
                    print(f"First interface: {f_data[0].get('ifDescr')}")
                    return True
                else:
                    print("Focused data is an empty list. (Device not polled yet or no ports found)")
            else:
                print("Focused data is MISSING from payload.")
                
            # Try sending a focus request
            print(f"Sending focus_switch request for {ip}...")
            await websocket.send(json.dumps({"type": "focus_switch", "ip": ip}))
            
            message = await websocket.recv()
            data = json.loads(message)
            print(f"Received update after request. Focused IP: {data.get('focused_ip')}")
            if data.get('focused_data') is not None:
                 print(f"Focused data received after request! Count: {len(data.get('focused_data'))}")
                 return True
                 
    except Exception as e:
        print(f"Error: {e}")
    return False

if __name__ == "__main__":
    ip_to_test = sys.argv[1] if len(sys.argv) > 1 else "10.160.4.1"
    success = asyncio.run(test_focus(ip_to_test))
    if success:
        print("\nSUCCESS: Data flow confirmed.")
        sys.exit(0)
    else:
        print("\nFAILURE: No data received.")
        sys.exit(1)
