import sys


def main():
    args = sys.argv[1:]
    if args[:1] == ["shout"]:
        print(" ".join(args[1:]).upper())
        return 0
    text = sys.stdin.read()
    print(text.strip()[::-1])
    return 0
