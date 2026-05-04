
import asyncio
import websockets

async def test():
    uri = "ws://10.153.88.242:8765/ws/esp32"
    print(f"Connecting to {uri}...")
    try:
        async with websockets.connect(uri) as websocket:
            print("Connected!")
            msg = await websocket.recv()
            print(f"Received: {msg}")
            
            # Send dummy frame header
            await websocket.send(bytes([0x02, 0x00]))
            print("Sent dummy frame")
            
            await asyncio.sleep(1)
            print("Done")
    except Exception as e:
        print(f"Failed: {e}")

asyncio.run(test())
