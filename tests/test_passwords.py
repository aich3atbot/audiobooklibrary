from app.passwords import hash_password, verify_password


def test_roundtrip():
    stored = hash_password("hunter2", n=2**4)
    assert stored.startswith("scrypt$16$8$1$")
    assert verify_password("hunter2", stored)


def test_wrong_password_rejected():
    stored = hash_password("hunter2", n=2**4)
    assert not verify_password("hunter3", stored)
    assert not verify_password("", stored)


def test_unique_salts():
    assert hash_password("hunter2", n=2**4) != hash_password("hunter2", n=2**4)


def test_parameters_come_from_the_stored_hash():
    # verification must honour embedded params, not current defaults
    stored = hash_password("pw", n=2**5, r=4, p=2)
    assert verify_password("pw", stored)


def test_garbage_stored_values_rejected():
    assert not verify_password("pw", "")
    assert not verify_password("pw", "not-a-hash")
    assert not verify_password("pw", "bcrypt$something$else$entirely$aa$bb")
    assert not verify_password("pw", "scrypt$16$8$1$zz$zz")  # invalid hex
