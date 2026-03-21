import zmq, msgpack, time
ctx = zmq.Context()
sock = ctx.socket(zmq.PUSH)
sock.connect("tcp://127.0.0.1:5555")
time.sleep(1)
msg = msgpack.packb({"symbol": "BTCUSDT", "intent": "ENTER_LONG", "quantity": 0.01, "max_chase_usd": 10.0, "urgency": 1.0, "max_slippage_bps": 5.0, "exposure_scale": 1.0})
sock.send(msg)
print("Sent instruction!")

