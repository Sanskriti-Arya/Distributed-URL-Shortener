"""Short-code generation.

Counter-based: a Postgres sequence hands out unique ids (no collisions, no retry loop),
and we encode the id as 7 base62 characters. To stop codes from being guessable/enumerable
(aaaaaab, aaaaaac, ...), the id is first multiplied by a constant that is coprime to 62^7.
Multiplication by a unit modulo N is a bijection, so uniqueness is preserved.

62^7 = 3.52e12 codes, ~96 years of capacity at 10M new URLs/day.
"""
from math import gcd

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE = len(ALPHABET)
LENGTH = 7
SPACE = BASE**LENGTH
MULTIPLIER = 1_000_000_007  # prime -> coprime to 62 and 62^7
assert gcd(MULTIPLIER, SPACE) == 1
_INVERSE = pow(MULTIPLIER, -1, SPACE)


def encode(n: int) -> str:
    chars = []
    for _ in range(LENGTH):
        n, r = divmod(n, BASE)
        chars.append(ALPHABET[r])
    return "".join(reversed(chars))


def decode(code: str) -> int:
    n = 0
    for ch in code:
        n = n * BASE + ALPHABET.index(ch)
    return n


def code_from_id(i: int) -> str:
    if not 0 <= i < SPACE:
        raise ValueError("id outside code space")
    return encode((i * MULTIPLIER) % SPACE)


def id_from_code(code: str) -> int:
    return (decode(code) * _INVERSE) % SPACE
