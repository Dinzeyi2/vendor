import sys


def main():
    for line in sys.stdin:
        print(line.rstrip("\n")[::-1])
    return 0
