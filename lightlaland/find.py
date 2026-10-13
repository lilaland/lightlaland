"""python -m lightlaland.find  ->  list the Art-Net nodes that answer on this network."""
import sys

from .artnet import discover


def main():
    try:
        mine, nodes = discover(3.0)
    except OSError as e:
        print(f"Couldn't listen on UDP port 6454 ({e}). Quit other lighting apps and try again.")
        return 2
    print(f"This Mac: {mine or 'no network?'}\n")
    if not nodes:
        print("No Art-Net node answered.\n"
              "A node on a different IP range (often 2.x.x.x) answers to its own range,\n"
              "so this Mac may not hear it. lightlaland's broadcast setting still reaches it.")
        return 1
    for n in nodes:
        print(f"  {n['ip']:<16} {n['long'] or n['short'] or '(no name)'}")
        print(f"  {'':<16} DMX output universes: {', '.join(map(str, n['outputs'])) or 'none reported'}")
        if n["report"]:
            print(f"  {'':<16} status: {n['report']}")
        if not n["same_range"]:
            print(f"  {'':<16} different IP range from this Mac: use broadcast, or change its IP")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
