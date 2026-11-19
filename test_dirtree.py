import os
import struct
import tempfile

import _fastindex
from dirtree import Index, existing, human, scan

CLOSE, CREATE, DELETE, RENAME = 0x80000000, 0x100, 0x200, 0x1000 | 0x2000


def write(path, n):
    with open(path, "wb") as f:
        f.write(b"x" * n)


def usn(frn, parent, name, is_dir, reason=0):
    """가짜 USN_RECORD_V2 (8바이트 정렬)."""
    nb = name.encode("utf-16-le")
    rl = (60 + len(nb) + 7) // 8 * 8
    head = struct.pack("<IHHQQqqIIIIHH", rl, 2, 0, frn, parent, 0, 0, reason, 0, 0, 0x10 if is_dir else 0x20, len(nb), 60)
    return (head + nb).ljust(rl, b"\0")


def buf(*recs):
    return b"\0" * 8 + b"".join(recs)


def every(names):
    return sorted(names.path(i) for i in names.search([("*", 1)], 10**9)[1])


def test():
    # 폴더 스캔 모드 + 검색 문법
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "a", "b"))
        write(os.path.join(d, "top.txt"), 5)
        write(os.path.join(d, "a", "x.txt"), 10)
        write(os.path.join(d, "a", "b", "report.PDF"), 20)
        write(os.path.join(d, "a", "한글문서.TXT"), 1)

        n = scan(d)
        assert (n.size, n.files) == (36, 4)
        a = n.children[0]
        assert (a.size, a.files) == (31, 3)

        idx = Index()
        idx.add_tree(d)

        def names(q):
            return sorted(idx.name(h) for h in idx.search(q)[1])

        assert names("txt") == ["top.txt", "x.txt", "한글문서.TXT"]
        assert names("rep pdf") == ["report.PDF"]          # AND, 대소문자 무시
        assert names("*.pdf") == ["report.PDF"]            # 와일드카드
        assert names("r?port.*") == ["report.PDF"]
        assert names("한글") == ["한글문서.TXT"]
        assert names("\\b\\") == ["report.PDF"]            # 경로 검색
        assert [idx.path(h) for h in idx.search("report")[1]] == [os.path.join(d, "a", "b", "report.PDF")]
        assert idx.search("")[0] == 0
        assert existing(os.path.join(d, "a", "gone", "deeper")) == os.path.join(d, "a")

    # MFT 버퍼: 자식이 부모보다 먼저 와도, 부모를 몰라도(→ 루트), 루트 자기 자신은 제외
    v = _fastindex.Names("C:\\")
    assert v.apply(buf(usn(5, 5, ".", True), usn(30, 11, "r.pdf", False), usn(10, 5, "a", True),
                       usn(11, 10, "b", True), usn(40, 999, "o.txt", False))) == 5
    assert every(v) == ["C:\\a", "C:\\a\\b", "C:\\a\\b\\r.pdf", "C:\\o.txt"], every(v)

    # USN 저널: 생성(새 폴더 안에 새 파일), 폴더 이름 변경 → 하위 경로 자동 반영, 삭제, 임시 파일
    v.apply(buf(usn(50, 10, "new", True, CREATE | CLOSE),
                usn(51, 50, "n.txt", False, CREATE | CLOSE),
                usn(10, 5, "A2", True, RENAME | CLOSE),
                usn(40, 999, "o.txt", False, DELETE | CLOSE),
                usn(60, 5, "tmp", False, CREATE | DELETE | CLOSE),
                usn(11, 10, "b", True, 0x4 | CLOSE)), True)  # 내용만 바뀜 → 무시
    assert every(v) == ["C:\\A2", "C:\\A2\\b", "C:\\A2\\b\\r.pdf", "C:\\A2\\new", "C:\\A2\\new\\n.txt"], every(v)
    assert len(v) == 5

    # 해시 테이블 확장 + 대량 삭제(지운 칸 재사용)
    big = _fastindex.Names("D:\\")
    for i in range(1, 200_001):
        big.add(i, 0 if i <= 100 else i % 100 + 1, f"f{i}", i <= 100)
    assert len(big) == 200_000 and big.path(150) == "D:\\f52\\f151"
    big.apply(buf(*[usn(i, 0, "", False, DELETE) for i in range(101, 150_001)]), True)
    assert len(big) == 50_100 and big.search([("f150001", 0)], 10)[0] == 1 and big.search([("f149999", 0)], 10)[0] == 0
    before = big.memory()
    big.compact()  # 줄인 뒤에도 찾기·추가·경로가 그대로 동작해야 함
    assert big.memory() < before and big.path(150) == "D:\\f52\\f151" and big.search([("f150001", 0)], 10)[0] == 1
    for i in range(300_001, 400_001):
        big.add(i, 1, f"g{i}", False)
    assert len(big) == 150_100 and big.path(big.search([("g400000", 0)], 1)[1][0]) == "D:\\f1\\g400000"

    assert human(1536) == "1.5 KB"
    print("ok")


if __name__ == "__main__":
    test()
