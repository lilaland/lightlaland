"""Art-Net over UDP: sending DMX (ArtDMX) and finding nodes (ArtPoll / ArtPollReply).

Header layout trips people up because it mixes byte orders: the OpCode and the
universe are little-endian, while the protocol version and data length are big-endian.
"""
import socket
import struct
import time

PORT = 6454
BROADCAST = "broadcast"          # a fixture "ip" meaning: every node on the network
_HEADER = b"Art-Net\x00" + struct.pack("<H", 0x5000) + struct.pack(">H", 14)
ARTPOLL = b"Art-Net\x00" + struct.pack("<H", 0x2000) + struct.pack(">H", 14) + b"\x00\x00"


def artdmx(universe, data, sequence=0, physical=0):
    """Build one ArtDMX packet. `universe` is the 15-bit Port-Address (Net:SubNet:Universe)."""
    if not 0 <= universe < 32768:
        raise ValueError("universe must be 0..32767")
    data = bytes(data[:512])
    if len(data) < 2:
        data = data.ljust(2, b"\x00")
    if len(data) % 2:
        data += b"\x00"
    return (_HEADER + bytes([sequence & 0xFF, physical & 0xFF])
            + struct.pack("<H", universe) + struct.pack(">H", len(data)) + data)


class Sender:
    """Sends one universe per (target, universe) every frame.

    A node IP gets a normal unicast packet with all 512 channels. "broadcast" goes to
    255.255.255.255, which every node hears whatever IP range it's set to; only the
    channels in use are sent, to keep the load on Wi-Fi small.
    """

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.seq = {}
        self.errors = {}
        self.packets = 0

    def send(self, ip, universe, data, used=512):
        if not self.enabled:
            return
        key = (ip, universe)
        seq = self.seq.get(key, 0) % 255 + 1  # 1..255; 0 would disable sequencing
        self.seq[key] = seq
        if ip == BROADCAST:
            dest, data = "255.255.255.255", data[:max(2, used)]
        else:
            dest = ip
        try:
            self.sock.sendto(artdmx(universe, data, seq), (dest, PORT))
            self.errors.pop(key, None)
            self.packets += 1
        except OSError as e:  # unreachable node, bad IP, no network...
            self.errors[key] = str(e)


# --- finding nodes -----------------------------------------------------------------------
def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # sends nothing; just picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def parse_reply(data):
    """An ArtPollReply packet -> dict, or None if the packet is anything else."""
    if len(data) < 207 or data[:8] != b"Art-Net\x00" or data[8:10] != b"\x00\x21":
        return None

    def text(b):
        return b.split(b"\x00", 1)[0].decode("latin-1").strip()

    net, sub = data[18] & 0x7F, data[19] & 0x0F
    outputs = [(net << 8) | (sub << 4) | (data[190 + i] & 0x0F)
               for i in range(min(4, data[173]))
               if data[174 + i] & 0x80]          # bit 7: this port outputs DMX
    return {
        "ip": ".".join(str(b) for b in data[10:14]),
        "short": text(data[26:44]),
        "long": text(data[44:108]),
        "report": text(data[108:172]),
        "outputs": outputs,
        "mac": ":".join(f"{b:02x}" for b in data[201:207]),
    }


def same_range(a, b):
    """Rough check for a typical home network (/24): do two IPs share the first three parts?"""
    return bool(a and b) and a.rsplit(".", 1)[0] == b.rsplit(".", 1)[0]


def discover(seconds=2.5, targets=None):
    """Broadcast ArtPoll for a few seconds; return (this machine's IP, [nodes])."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", PORT))  # nodes answer on port 6454
    sock.settimeout(0.2)
    mine = local_ip()
    if targets is None:
        targets = ["255.255.255.255"]
        if mine:
            targets.append(mine.rsplit(".", 1)[0] + ".255")
    found = {}
    end, next_poll = time.time() + seconds, 0.0
    try:
        while time.time() < end:
            if time.time() >= next_poll:  # poll every second in case a packet gets lost
                for t in targets:
                    try:
                        sock.sendto(ARTPOLL, (t, PORT))
                    except OSError:
                        pass
                next_poll = time.time() + 1.0
            try:
                data, _ = sock.recvfrom(2048)
            except socket.timeout:
                continue
            reply = parse_reply(data)
            if reply:  # nodes with more than 4 ports answer once per group of 4
                key = (reply["ip"], reply["mac"])
                if key in found:
                    found[key]["outputs"] = sorted(set(found[key]["outputs"] + reply["outputs"]))
                else:
                    found[key] = dict(reply, same_range=same_range(mine, reply["ip"]))
    finally:
        sock.close()
    return mine, list(found.values())
