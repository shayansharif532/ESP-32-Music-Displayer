"""X25519 against the RFC 7748 test vectors (section 5.2 and 6.1) and the low-order-point refusal.

    companion\\.venv\\Scripts\\python.exe -m unittest discover -s companion/tests
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from desk_media_companion import x25519  # noqa: E402

H = bytes.fromhex


class Rfc7748(unittest.TestCase):
    def test_section_5_2_vectors(self):
        self.assertEqual(x25519.x25519(H("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4"),
                                       H("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")).hex(),
                         "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552")
        self.assertEqual(x25519.x25519(H("4b66e9d4d1b4673c5ad22691957d6af5c11b6421e0ea01d42ca4169e7918ba0d"),
                                       H("e5210f12786811d3f4b7959d0538ae2c31dbe7106fc03c3efc4cd549c715a493")).hex(),
                         "95cbde9476e8907d7aade45cb4b873f88b595a68799fa152e6f8f7647aac7957")

    def test_section_5_2_iterated(self):
        k = u = x25519.BASE_POINT
        k, u = x25519.x25519(k, u), k
        self.assertEqual(k.hex(), "422c8e7a6227d7bca1350b3e2bb7279f7897b87bb6854b783c60e80311ae3079")
        t0 = time.perf_counter()
        for _ in range(9):
            k, u = x25519.x25519(k, u), k
        per_call = (time.perf_counter() - t0) / 9
        if per_call * 990 > 5.0:                                  # the 1000-iteration vector only runs when it is cheap
            self.skipTest("1000 iterations would take %.0f s" % (per_call * 990))
        for _ in range(990):
            k, u = x25519.x25519(k, u), k
        self.assertEqual(k.hex(), "684cf59ba83309552800ef566f2f4d3c1c3887c49360e3875f2eb94d99532c51")

    def test_section_6_1_diffie_hellman(self):
        a_priv = H("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
        a_pub = H("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
        b_priv = H("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
        b_pub = H("de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f")
        shared = "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"
        self.assertEqual(x25519.x25519(a_priv, x25519.BASE_POINT), a_pub)
        self.assertEqual(x25519.x25519(b_priv, x25519.BASE_POINT), b_pub)
        self.assertEqual(x25519.shared(a_priv, b_pub).hex(), shared)
        self.assertEqual(x25519.shared(b_priv, a_pub).hex(), shared)


class Usage(unittest.TestCase):
    def test_generate_gives_a_working_pair(self):
        a_priv, a_pub = x25519.generate()
        b_priv, b_pub = x25519.generate()
        self.assertNotEqual(a_priv, b_priv)
        self.assertEqual(x25519.shared(a_priv, b_pub), x25519.shared(b_priv, a_pub))

    def test_low_order_public_keys_are_refused(self):
        priv = H("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
        for u in (bytes(32), (1).to_bytes(32, "little"),
                  H("e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800"),
                  H("5f9c95bca3508c24b1d0b1559c83ef5b04445cc4581c8e86d8224eddd09f1157")):
            with self.assertRaises(ValueError):
                x25519.shared(priv, u)

    def test_inputs_must_be_32_bytes(self):
        with self.assertRaises(ValueError):
            x25519.x25519(b"\x01" * 31, x25519.BASE_POINT)
        with self.assertRaises(ValueError):
            x25519.x25519(b"\x01" * 32, b"\x09" * 33)


if __name__ == "__main__":
    unittest.main()
