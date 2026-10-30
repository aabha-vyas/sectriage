"""Fail unless a pytest junit file shows tests that actually ran.

The sandbox fixtures call pytest.skip() when the Docker daemon or the sandbox image is missing, so
a broken image build would otherwise leave the docker job green with every test skipped.
Usage: python3 -I check_junit.py JUNIT.xml
"""

import sys
import xml.etree.ElementTree as ET


def main(path: str) -> int:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = sum(int(s.get("tests", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    print(f"docker tests: {total} collected, {skipped} skipped")
    if total == 0:
        print("error: no docker tests ran", file=sys.stderr)
        return 1
    if skipped:
        print("error: docker tests were skipped (daemon or image unavailable?)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: check_junit.py JUNIT.xml")
    sys.exit(main(sys.argv[1]))
