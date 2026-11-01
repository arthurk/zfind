"""Expected listings shared by tests."""


def _words(text: str) -> list[bytes]:
    return [word.encode() for word in text.split()]


IGNORES = _words("""
    .git/ .gitignore .hidden a.o a.txt build/ build/b1 build/b2 build/inner/
    build/inner/b3 c.o c.txt skipme sub/ sub/.gitignore sub/build sub/keep.log
    sub/local/ sub/local/l1 sub/noise.log sub/skipme sub/x.o sub/z.txt tmp1
    tmp_keep tmp_other z.txt
""")

IGNORES_GITIGNORE = _words("""
    .gitignore .hidden a.txt c.txt sub/ sub/.gitignore sub/build
    sub/keep.log sub/skipme sub/z.txt tmp_keep z.txt
""")

IGNORES_GITIGNORE_NO_HIDDEN = _words(
    "a.txt c.txt sub/ sub/build sub/keep.log sub/skipme sub/z.txt tmp_keep z.txt"
)
