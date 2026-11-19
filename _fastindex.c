/* DirTree 검색 인덱스 (C 확장).
 *
 * 드라이브 하나 = Names 객체 하나. 항목마다 (FRN, 부모 FRN, 이름)만 저장하고
 * 경로는 필요할 때 부모를 따라 올라가며 만든다 → 폴더 이름 변경/이동 시 하위 항목을 고칠 필요가 없다.
 * 이름은 하나의 큰 UTF-16 버퍼에 몰아 저장하고, 대소문자 무시 비교는 65536칸 소문자 표로 한다.
 * GIL을 놓지 않으므로 Python 스레드끼리 동시에 불러도 안전하다.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <string.h>

#define F_DIR 1
#define F_DEAD 2
#define TOMB 0xFFFFFFFFu
#define MAXPATH 32768
#define USN_DELETE 0x200u
#define USN_UPSERT (0x100u | 0x2000u) /* FILE_CREATE | RENAME_NEW_NAME */

static wchar_t LOWER[65536];

typedef struct {
    PyObject_HEAD
    uint64_t *frn, *parent;
    uint32_t *off;
    uint16_t *len;
    uint8_t *flags;
    Py_ssize_t n, cap, alive;
    wchar_t *names;
    size_t nlen, ncap;
    uint32_t *table; /* 열린 주소 해시: FRN → 항목 번호 + 1 (0 = 빈칸, TOMB = 지운 칸) */
    size_t tcap, tused;
    wchar_t *root, *scratch;
    size_t rootlen;
} Names;

static size_t hslot(uint64_t k, size_t mask)
{
    k ^= k >> 33;
    k *= 0xff51afd7ed558ccdULL;
    k ^= k >> 33;
    return (size_t)k & mask;
}

static Py_ssize_t find(Names *s, uint64_t frn)
{
    if (!s->tcap)
        return -1;
    size_t m = s->tcap - 1, h = hslot(frn, m);
    for (;;) {
        uint32_t v = s->table[h];
        if (!v)
            return -1;
        if (v != TOMB && s->frn[v - 1] == frn)
            return (Py_ssize_t)v - 1;
        h = (h + 1) & m;
    }
}

static int rehash(Names *s, size_t factor) /* 칸 수 >= 살아 있는 항목 × factor (2의 거듭제곱) */
{
    size_t cap = 1024;
    while (cap < (size_t)s->alive * factor)
        cap <<= 1;
    uint32_t *t = PyMem_Calloc(cap, sizeof(uint32_t));
    if (!t) {
        PyErr_NoMemory();
        return -1;
    }
    size_t m = cap - 1;
    for (Py_ssize_t i = 0; i < s->n; i++) {
        if (s->flags[i] & F_DEAD)
            continue;
        size_t h = hslot(s->frn[i], m);
        while (t[h])
            h = (h + 1) & m;
        t[h] = (uint32_t)i + 1;
    }
    PyMem_Free(s->table);
    s->table = t;
    s->tcap = cap;
    s->tused = (size_t)s->alive;
    return 0;
}

#define GROW(field, type)                                         \
    {                                                             \
        type *p = PyMem_Realloc(s->field, nc * sizeof(type));     \
        if (!p) {                                                 \
            PyErr_NoMemory();                                     \
            return -1;                                            \
        }                                                         \
        s->field = p;                                             \
    }

static int grow_records(Names *s)
{
    Py_ssize_t nc = s->cap ? s->cap + s->cap / 2 : 65536;
    GROW(frn, uint64_t)
    GROW(parent, uint64_t)
    GROW(off, uint32_t)
    GROW(len, uint16_t)
    GROW(flags, uint8_t)
    s->cap = nc;
    return 0;
}

static int upsert(Names *s, uint64_t frn, uint64_t parent, const wchar_t *name, size_t nl, int is_dir)
{
    if (nl > 0xFFFF)
        nl = 0xFFFF;
    Py_ssize_t i = find(s, frn);
    if (i < 0) {
        if ((s->tused + 1) * 2 > s->tcap && rehash(s, 4) < 0)
            return -1;
        if (s->n == s->cap && grow_records(s) < 0)
            return -1;
        i = s->n++;
        s->frn[i] = frn;
        size_t m = s->tcap - 1, h = hslot(frn, m);
        while (s->table[h] && s->table[h] != TOMB)
            h = (h + 1) & m;
        if (!s->table[h])
            s->tused++;
        s->table[h] = (uint32_t)i + 1;
        s->alive++;
        s->len[i] = 0xFFFF; /* 아래 이름 비교가 실패하도록 */
    }
    if (s->len[i] != nl || memcmp(s->names + s->off[i], name, nl * sizeof(wchar_t))) {
        if (s->nlen + nl > s->ncap) {
            size_t nc = s->ncap ? s->ncap : (1 << 20);
            while (nc < s->nlen + nl)
                nc += nc / 2;
            wchar_t *p = PyMem_Realloc(s->names, nc * sizeof(wchar_t));
            if (!p) {
                PyErr_NoMemory();
                return -1;
            }
            s->names = p;
            s->ncap = nc;
        }
        memcpy(s->names + s->nlen, name, nl * sizeof(wchar_t));
        s->off[i] = (uint32_t)s->nlen;
        s->len[i] = (uint16_t)nl;
        s->nlen += nl;
    }
    s->parent[i] = parent;
    s->flags[i] = is_dir ? F_DIR : 0;
    return 0;
}

static void remove_frn(Names *s, uint64_t frn)
{
    if (!s->tcap)
        return;
    size_t m = s->tcap - 1, h = hslot(frn, m);
    for (;;) {
        uint32_t v = s->table[h];
        if (!v)
            return;
        if (v != TOMB && s->frn[v - 1] == frn) {
            s->table[h] = TOMB;
            s->flags[v - 1] |= F_DEAD;
            s->alive--;
            return;
        }
        h = (h + 1) & m;
    }
}

static size_t build_path(Names *s, Py_ssize_t i, wchar_t *out)
{
    Py_ssize_t chain[512];
    int d = 0;
    for (Py_ssize_t cur = i; cur >= 0 && d < 512;) { /* 512 = 손상된 순환 방지 */
        chain[d++] = cur;
        Py_ssize_t nx = find(s, s->parent[cur]);
        if (nx == cur)
            break;
        cur = nx; /* 부모를 모르면 -1 → 드라이브 루트 */
    }
    size_t L = s->rootlen;
    memcpy(out, s->root, L * sizeof(wchar_t));
    for (int k = d - 1; k >= 0; k--) {
        Py_ssize_t c = chain[k];
        size_t nl = s->len[c];
        if (L + nl + 2 >= MAXPATH)
            break;
        if (L && out[L - 1] != L'\\')
            out[L++] = L'\\';
        memcpy(out + L, s->names + s->off[c], nl * sizeof(wchar_t));
        L += nl;
    }
    return L;
}

/* 대소문자 무시 부분 문자열 (pat은 이미 소문자) */
static int contains(const wchar_t *h, size_t hl, const wchar_t *p, size_t pl)
{
    if (!pl)
        return 1;
    for (size_t i = 0; i + pl <= hl; i++) {
        if (LOWER[h[i]] != p[0])
            continue;
        size_t k = 1;
        while (k < pl && LOWER[h[i + k]] == p[k])
            k++;
        if (k == pl)
            return 1;
    }
    return 0;
}

/* 대소문자 무시 와일드카드 * ? (전체 일치) */
static int glob(const wchar_t *s, size_t sl, const wchar_t *p, size_t pl)
{
    size_t si = 0, pi = 0, star = (size_t)-1, mark = 0;
    while (si < sl) {
        if (pi < pl && p[pi] == L'*') {
            star = pi++;
            mark = si;
        } else if (pi < pl && (p[pi] == L'?' || p[pi] == LOWER[s[si]])) {
            si++;
            pi++;
        } else if (star != (size_t)-1) {
            pi = star + 1;
            si = ++mark;
        } else {
            return 0;
        }
    }
    while (pi < pl && p[pi] == L'*')
        pi++;
    return pi == pl;
}

/* ---------------------------------------------------------------- Python 메서드 */

static Py_ssize_t check_index(Names *s, Py_ssize_t i)
{
    if (i < 0 || i >= s->n) {
        PyErr_SetString(PyExc_IndexError, "Names index out of range");
        return -1;
    }
    return i;
}

static PyObject *Names_apply(Names *s, PyObject *args)
{
    Py_buffer b;
    int journal = 0;
    if (!PyArg_ParseTuple(args, "y*|p", &b, &journal))
        return NULL;
    const uint8_t *p = b.buf;
    size_t L = (size_t)b.len, pos = 8;
    Py_ssize_t count = 0;
    int err = 0;
    while (pos + 60 <= L) {
        uint32_t rl, reason, attrs;
        uint16_t major, nlb, no;
        uint64_t frn, par;
        memcpy(&rl, p + pos, 4);
        memcpy(&major, p + pos + 4, 2);
        if (rl < 60 || pos + rl > L)
            break;
        if (major == 2) { /* USN_RECORD_V2 */
            memcpy(&frn, p + pos + 8, 8);
            memcpy(&par, p + pos + 16, 8);
            memcpy(&reason, p + pos + 40, 4);
            memcpy(&attrs, p + pos + 52, 4);
            memcpy(&nlb, p + pos + 56, 2);
            memcpy(&no, p + pos + 58, 2);
            if ((size_t)no + nlb <= rl && frn != par) { /* frn == par: 루트 폴더 자신 */
                if (journal && (reason & USN_DELETE)) {
                    remove_frn(s, frn);
                } else if (!journal || (reason & USN_UPSERT)) {
                    if (upsert(s, frn, par, (const wchar_t *)(p + pos + no), nlb / 2, attrs & 0x10) < 0) {
                        err = 1;
                        break;
                    }
                }
            }
            count++;
        }
        pos += rl;
    }
    PyBuffer_Release(&b);
    return err ? NULL : PyLong_FromSsize_t(count);
}

static PyObject *Names_add(Names *s, PyObject *args)
{
    unsigned long long frn, par;
    PyObject *name;
    int is_dir;
    if (!PyArg_ParseTuple(args, "KKUp", &frn, &par, &name, &is_dir))
        return NULL;
    Py_ssize_t nl;
    wchar_t *w = PyUnicode_AsWideCharString(name, &nl);
    if (!w)
        return NULL;
    int r = frn == par ? 0 : upsert(s, frn, par, w, (size_t)nl, is_dir);
    PyMem_Free(w);
    if (r < 0)
        return NULL;
    Py_RETURN_NONE;
}

typedef struct {
    wchar_t *w;
    Py_ssize_t n;
    int kind; /* 1 = 와일드카드, 2 = 전체 경로 대상 */
} Term;

static PyObject *Names_search(Names *s, PyObject *args)
{
    PyObject *terms, *seq, *hits = NULL, *result = NULL;
    Py_ssize_t limit, nt, total = 0, k;
    if (!PyArg_ParseTuple(args, "On", &terms, &limit))
        return NULL;
    if (!(seq = PySequence_Fast(terms, "terms must be a sequence of (str, int)")))
        return NULL;
    nt = PySequence_Fast_GET_SIZE(seq);
    Term *t = PyMem_Calloc(nt ? nt : 1, sizeof(Term));
    if (!t) {
        PyErr_NoMemory();
        goto done;
    }
    for (k = 0; k < nt; k++) {
        PyObject *str;
        if (!PyArg_ParseTuple(PySequence_Fast_GET_ITEM(seq, k), "Ui", &str, &t[k].kind))
            goto done;
        if (!(t[k].w = PyUnicode_AsWideCharString(str, &t[k].n)))
            goto done;
    }
    if (!(hits = PyList_New(0)))
        goto done;
    for (Py_ssize_t i = 0; i < s->n; i++) {
        if (s->flags[i] & F_DEAD)
            continue;
        size_t plen = (size_t)-1;
        int ok = 1;
        for (k = 0; k < nt && ok; k++) {
            const wchar_t *h;
            size_t hl;
            if (t[k].kind & 2) {
                if (plen == (size_t)-1)
                    plen = build_path(s, i, s->scratch);
                h = s->scratch;
                hl = plen;
            } else {
                h = s->names + s->off[i];
                hl = s->len[i];
            }
            ok = (t[k].kind & 1) ? glob(h, hl, t[k].w, (size_t)t[k].n) : contains(h, hl, t[k].w, (size_t)t[k].n);
        }
        if (!ok)
            continue;
        total++;
        if (PyList_GET_SIZE(hits) < limit) {
            PyObject *v = PyLong_FromSsize_t(i);
            if (!v || PyList_Append(hits, v) < 0) {
                Py_XDECREF(v);
                goto done;
            }
            Py_DECREF(v);
        }
    }
    result = Py_BuildValue("(nO)", total, hits);
done:
    if (t)
        for (k = 0; k < nt; k++)
            PyMem_Free(t[k].w);
    PyMem_Free(t);
    Py_XDECREF(hits);
    Py_DECREF(seq);
    return result;
}

static PyObject *Names_path(Names *s, PyObject *arg)
{
    Py_ssize_t i = PyLong_AsSsize_t(arg);
    if ((i == -1 && PyErr_Occurred()) || check_index(s, i) < 0)
        return NULL;
    size_t L = build_path(s, i, s->scratch);
    return PyUnicode_FromWideChar(s->scratch, (Py_ssize_t)L);
}

static PyObject *Names_name(Names *s, PyObject *arg)
{
    Py_ssize_t i = PyLong_AsSsize_t(arg);
    if ((i == -1 && PyErr_Occurred()) || check_index(s, i) < 0)
        return NULL;
    return PyUnicode_FromWideChar(s->names + s->off[i], s->len[i]);
}

static PyObject *Names_memory(Names *s, PyObject *Py_UNUSED(ignored))
{
    size_t per = 2 * sizeof(uint64_t) + sizeof(uint32_t) + sizeof(uint16_t) + sizeof(uint8_t);
    return PyLong_FromSize_t(s->cap * per + s->ncap * sizeof(wchar_t) + s->tcap * sizeof(uint32_t));
}

/* 인덱싱이 끝난 뒤 한 번: 늘리면서 생긴 여유 공간을 실제 크기 + 약 6%로 줄인다.
 * 작게 다시 할당하다 실패하면 그 배열은 원래(더 큰) 블록 그대로라 cap 이상이 보장된다. */
#define SHRINK(field, type, count)                                       \
    {                                                                    \
        type *p = PyMem_Realloc(s->field, (count) * sizeof(type));       \
        if (p)                                                           \
            s->field = p;                                                \
    }

static PyObject *Names_compact(Names *s, PyObject *Py_UNUSED(ignored))
{
    Py_ssize_t nc = s->n + s->n / 16 + 1024;
    if (nc < s->cap) {
        SHRINK(frn, uint64_t, nc)
        SHRINK(parent, uint64_t, nc)
        SHRINK(off, uint32_t, nc)
        SHRINK(len, uint16_t, nc)
        SHRINK(flags, uint8_t, nc)
        s->cap = nc;
    }
    size_t nn = s->nlen + s->nlen / 16 + 4096;
    if (nn < s->ncap) {
        SHRINK(names, wchar_t, nn)
        s->ncap = nn;
    }
    if (rehash(s, 2) < 0) /* 해시 칸: 항목의 4배 → 2배 (채움률 25~50%) */
        return NULL;
    Py_RETURN_NONE;
}

static Py_ssize_t Names_len(Names *s) { return s->alive; }

static PyObject *Names_new(PyTypeObject *type, PyObject *args, PyObject *kw)
{
    PyObject *root;
    if (!PyArg_ParseTuple(args, "U", &root))
        return NULL;
    Names *s = (Names *)type->tp_alloc(type, 0);
    if (!s)
        return NULL;
    Py_ssize_t rl;
    s->root = PyUnicode_AsWideCharString(root, &rl);
    s->rootlen = (size_t)rl;
    s->scratch = PyMem_Malloc(MAXPATH * sizeof(wchar_t));
    if (!s->root || !s->scratch || s->rootlen >= MAXPATH / 2) {
        Py_DECREF(s);
        return PyErr_Occurred() ? NULL : PyErr_NoMemory();
    }
    return (PyObject *)s;
}

static void Names_dealloc(Names *s)
{
    PyMem_Free(s->frn);
    PyMem_Free(s->parent);
    PyMem_Free(s->off);
    PyMem_Free(s->len);
    PyMem_Free(s->flags);
    PyMem_Free(s->names);
    PyMem_Free(s->table);
    PyMem_Free(s->root);
    PyMem_Free(s->scratch);
    Py_TYPE(s)->tp_free((PyObject *)s);
}

static PyMethodDef Names_methods[] = {
    {"apply", (PyCFunction)Names_apply, METH_VARARGS,
     "apply(buf, journal=False) -> 처리한 레코드 수. MFT 열거/USN 저널 버퍼를 반영."},
    {"add", (PyCFunction)Names_add, METH_VARARGS, "add(frn, parent_frn, name, is_dir) — 폴더 스캔용."},
    {"search", (PyCFunction)Names_search, METH_VARARGS,
     "search([(소문자 단어, 종류)], limit) -> (일치 수, [번호]). 종류: 1 = 와일드카드, 2 = 전체 경로."},
    {"path", (PyCFunction)Names_path, METH_O, "path(i) -> 전체 경로"},
    {"name", (PyCFunction)Names_name, METH_O, "name(i) -> 이름"},
    {"memory", (PyCFunction)Names_memory, METH_NOARGS, "memory() -> 할당한 바이트 수"},
    {"compact", (PyCFunction)Names_compact, METH_NOARGS, "compact() — 인덱싱 후 여유 공간 정리"},
    {NULL},
};

static PySequenceMethods Names_seq = {.sq_length = (lenfunc)Names_len};

static PyTypeObject NamesType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "_fastindex.Names",
    .tp_basicsize = sizeof(Names),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = "Names(root) — 드라이브 하나의 파일명 인덱스",
    .tp_new = Names_new,
    .tp_dealloc = (destructor)Names_dealloc,
    .tp_methods = Names_methods,
    .tp_as_sequence = &Names_seq,
};

static PyObject *set_lower(PyObject *self, PyObject *arg)
{
    Py_buffer b;
    if (PyObject_GetBuffer(arg, &b, PyBUF_SIMPLE) < 0)
        return NULL;
    if (b.len != sizeof(LOWER)) {
        PyBuffer_Release(&b);
        return PyErr_Format(PyExc_ValueError, "need %d bytes", (int)sizeof(LOWER));
    }
    memcpy(LOWER, b.buf, sizeof(LOWER));
    PyBuffer_Release(&b);
    Py_RETURN_NONE;
}

static PyMethodDef module_methods[] = {
    {"set_lower", set_lower, METH_O, "set_lower(bytes) — 65536칸 UTF-16 소문자 표 (Python str.lower 와 맞추기 위함)"},
    {NULL},
};

static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "_fastindex", NULL, -1, module_methods};

PyMODINIT_FUNC PyInit__fastindex(void)
{
    for (int c = 0; c < 65536; c++)
        LOWER[c] = (wchar_t)((c >= 'A' && c <= 'Z') ? c + 32 : c);
    if (PyType_Ready(&NamesType) < 0)
        return NULL;
    PyObject *m = PyModule_Create(&module);
    if (!m)
        return NULL;
    if (PyModule_AddObjectRef(m, "Names", (PyObject *)&NamesType) < 0) {
        Py_DECREF(m);
        return NULL;
    }
    return m;
}
