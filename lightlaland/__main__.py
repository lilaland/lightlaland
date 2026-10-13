"""python -m lightlaland  ->  runs the show and serves the control page."""
import argparse
import os
import socket
import threading

from .app import App
from .server import serve


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; this just picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def report_search(app):
    res = app.search(2.5)
    if res["error"]:
        print(f"  Art-Net search failed: {res['error']}")
    elif not res["nodes"]:
        print("  Art-Net search: no node answered. One on another IP range (like 2.x.x.x)\n"
              "  can't answer this Mac, but lights set to broadcast still reach it.")
    for n in res["nodes"]:
        where = "" if n["same_range"] else "  (different IP range: use broadcast)"
        print(f"  Art-Net node:      {n['long'] or n['short']} at {n['ip']}, "
              f"universes {', '.join(map(str, n['outputs']))}{where}")


def main():
    ap = argparse.ArgumentParser(prog="lightlaland", description=__doc__)
    ap.add_argument("--config", default=os.path.join(os.getcwd(), "lightlaland.json"),
                    help="settings and calibration file (default: ./lightlaland.json)")
    ap.add_argument("--port", type=int, help="web UI port (default 8080)")
    ap.add_argument("--wav", help="play and analyse a WAV file instead of the sound card input")
    ap.add_argument("--no-audio", action="store_true", help="no audio input: tap tempo only")
    ap.add_argument("--dry-run", action="store_true", help="don't send any Art-Net")
    args = ap.parse_args()

    app = App(args.config, wav=args.wav, no_audio=args.no_audio, dry_run=args.dry_run)
    port = args.port or app.config["server"]["port"]
    server = serve(app, app.config["server"]["host"], port)
    app.start()

    src = app.source
    print(f"\n  lightlaland running   (settings: {args.config})")
    print(f"  on this Mac:       http://localhost:{port}")
    print(f"  on the iPad:       http://{lan_ip()}:{port}   or   http://{socket.gethostname()}:{port}")
    if src is None:
        print("  audio:             off (tap tempo)")
    elif src.error:
        print(f"  audio:             NOT RUNNING - {src.error}")
    else:
        print(f"  audio:             {src.name}")
    for fx in app.config["fixtures"]:
        print(f"  {fx['id']} {fx['name']:24s} -> Art-Net {fx['ip']} universe {fx['universe']}, "
              f"address {fx['address']}")
    if args.dry_run:
        print("  Art-Net:           dry run, nothing sent")
    print("\n  Ctrl+C to stop.\n")
    threading.Thread(target=report_search, args=(app,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        server.server_close()


if __name__ == "__main__":
    main()
