from app.codegen import LENGTH, SPACE, code_from_id, id_from_code


def test_length_and_alphabet():
    for i in (0, 1, 61, 62, 10**6, 10**12):
        code = code_from_id(i)
        assert len(code) == LENGTH and code.isalnum()


def test_roundtrip():
    for i in (0, 1, 12345, 987654321, SPACE - 1):
        assert id_from_code(code_from_id(i)) == i


def test_no_collisions_in_sample():
    codes = {code_from_id(i) for i in range(1, 200_000)}
    assert len(codes) == 199_999


def test_sequential_ids_not_adjacent():
    a, b = code_from_id(1000), code_from_id(1001)
    assert a != b and a[:3] != b[:3]  # consecutive ids must not look consecutive
