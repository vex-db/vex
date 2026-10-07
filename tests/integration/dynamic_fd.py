#!/usr/bin/env python3
"""Hold >8192 clients, verify old/new connections, then verify descriptor reuse.

Run against a dedicated empty Vex server with OS nofile limits above 10000.
"""
import argparse
import socket

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--host', default='127.0.0.1')
p.add_argument('--port', type=int, default=6380)
p.add_argument('--connections', type=int, default=9000)
o = p.parse_args()
clients = []

def ping(client):
    client.sendall(b'*1\r\n$4\r\nPING\r\n')
    data = bytearray()
    while len(data) < 7:
        chunk = client.recv(7 - len(data))
        assert chunk, 'connection closed during PING'
        data.extend(chunk)
    assert data == b'+PONG\r\n', data

try:
    for i in range(o.connections):
        client = socket.create_connection((o.host, o.port), timeout=10)
        clients.append(client)
        ping(client)
    for client in clients:
        ping(client)
    for i in range(0, len(clients), 2):
        clients[i].close()
        clients[i] = socket.create_connection((o.host, o.port), timeout=10)
    for client in clients:
        ping(client)
    print(f'PASS: {len(clients)} simultaneous clients, old/new PING responses, descriptor reuse')
finally:
    for client in clients:
        client.close()
