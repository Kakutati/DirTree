"""DirTree — Q-Dir 스타일 탭/분할 탐색기 + TreeSize 스타일 용량 트리 + Everything 스타일 파일 검색."""
import ctypes
import json
import os
import shutil
import string
import subprocess
import sys
import time
from ctypes import wintypes
from struct import pack, unpack_from

import _fastindex  # C 확장: python setup.py build_ext --inplace

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDockWidget, QFileDialog, QFileIconProvider, QFileSystemModel,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QSpinBox, QSplitter, QStyledItemDelegate,
    QTabWidget, QToolButton, QTreeView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

UR = Qt.UserRole
DEFAULT_DEPTH = 10
HOME = os.path.expanduser("~")
# C 인덱스의 대소문자 무시 비교를 Python str.lower()와 똑같이 맞춘다
_fastindex.set_lower("".join(c.lower() if len(c.lower()) == 1 else c for c in map(chr, range(65536)))
                     .encode("utf-16-le", "surrogatepass"))


# ---------------------------------------------------------------- 코어 (Qt 없음)

def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024


def existing(p):
    """p가 없으면 존재하는 가장 가까운 상위 폴더, 그것도 없으면 홈 폴더."""
    while p and not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            return HOME
        p = parent
    return p or HOME


class Node:
    """폴더 하나. 경로 문자열은 저장하지 않고 이름 + 부모만 둔다(루트의 name = 전체 경로). row = 부모 안에서의 순서."""
    __slots__ = ("name", "parent", "size", "files", "children", "denied", "row")

    def __init__(self, name, parent=None):
        self.name, self.parent, self.size, self.files, self.children, self.denied, self.row = \
            name, parent, 0, 0, [], False, 0

    @property
    def path(self):
        parts, n = [], self
        while n:
            parts.append(n.name)
            n = n.parent
        return os.path.join(*reversed(parts))


def scan(path, stop=lambda: False, tick=lambda: None, parent=None):
    """폴더를 재귀로 훑어 크기/파일 수를 합산. 정션·심볼릭 링크는 따라가지 않음(중복 집계·무한 루프 방지)."""
    node = Node(os.path.basename(path) if parent else path, parent)
    try:
        with os.scandir(path) as it:
            for e in it:
                if stop():
                    break
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.is_junction():
                            continue
                        child = scan(e.path, stop, tick, node)
                        child.row = len(node.children)
                        node.children.append(child)
                        node.size += child.size
                        node.files += child.files
                    else:
                        node.size += e.stat(follow_symlinks=False).st_size
                        node.files += 1
                except OSError:
                    pass
    except OSError:
        node.denied = True
    tick()
    return node


# --- Windows API (관리자 권한, MFT, 휴지통)

def is_admin():
    return os.name == "nt" and bool(ctypes.windll.shell32.IsUserAnAdmin())


def relaunch_as_admin():
    """UAC 창을 띄워 관리자 권한으로 다시 실행. 사용자가 취소하면 False."""
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw  # 콘솔 창 없이
    args = subprocess.list2cmdline([os.path.abspath(sys.argv[0])])
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 1) > 32


if os.name == "nt":
    K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    K32.CreateFileW.restype = wintypes.HANDLE
    K32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    K32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]


class Volume:
    """\\\\.\\C: 볼륨 핸들. 관리자 권한 필요, 실패 시 OSError."""

    def __init__(self, drive):
        self.h = K32.CreateFileW(f"\\\\.\\{drive[0]}:", 0x80000000, 3, None, 3, 0, None)  # GENERIC_READ, 읽기/쓰기 공유
        if self.h in (None, wintypes.HANDLE(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())

    def ioctl(self, code, inp, size):
        out, got = ctypes.create_string_buffer(size), wintypes.DWORD()
        buf = ctypes.create_string_buffer(inp, len(inp)) if inp else None
        if not K32.DeviceIoControl(self.h, code, buf, len(inp), out, size, ctypes.byref(got), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return out.raw[:got.value]

    def journal(self):
        """FSCTL_QUERY_USN_JOURNAL → (저널 ID, 다음 USN)."""
        jid, _, nxt = unpack_from("<Qqq", self.ioctl(0x900F4, b"", 64))
        return jid, nxt

    def read_journal(self, jid, start):
        """FSCTL_READ_USN_JOURNAL, 파일 핸들이 닫힐 때의 최종 기록만(ReturnOnlyOnClose), 기다리지 않음.
        (다음 USN, 원본 버퍼) — 버퍼 해석은 C 인덱스가 한다."""
        data = self.ioctl(0x900BB, pack("<qIIQQQ", start, 0xFFFFFFFF, 1, 0, 0, jid), 1 << 16)
        return unpack_from("<q", data)[0], data

    def close(self):
        K32.CloseHandle(self.h)


def mft_buffers(vol, stop):
    """NTFS 볼륨의 MFT를 통째로 읽는다(Everything과 같은 방식). 1 MB짜리 원본 버퍼를 차례로 내준다."""
    start = 0
    while not stop():
        try:
            data = vol.ioctl(0x900B3, pack("<Qqq", start, 0, 2**63 - 1), 1 << 20)  # FSCTL_ENUM_USN_DATA
        except OSError as e:
            if e.winerror == 38:  # ERROR_HANDLE_EOF = 끝까지 읽음
                return
            raise
        yield data
        start = unpack_from("<Q", data)[0]


def no_confirm_recycle():
    """휴지통 속성의 '삭제 확인 대화 상자 표시'가 꺼져 있으면 True (탐색기와 같은 동작을 위해)."""
    try:
        st = (ctypes.c_uint32 * 16)()
        ctypes.windll.shell32.SHGetSetSettings(st, 0x8000, False)  # SSF_NOCONFIRMRECYCLE
        return bool(st[0] & 4)
    except (AttributeError, OSError):
        return False


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


def delete_paths(paths, permanent, hwnd=None):
    """탐색기와 같은 셸 삭제: 기본은 휴지통, permanent=True면 즉시 삭제(항상 확인 창).
    (실제로 지워진 경로 목록, 삭제 전 파일 크기) 반환."""
    sizes = {}
    for p in paths:
        try:
            sizes[p] = os.stat(p, follow_symlinks=False).st_size
        except OSError:
            pass
    flags = 0
    if not permanent:
        flags = 0x40 | 0x4000 | (0x10 if no_confirm_recycle() else 0)  # ALLOWUNDO | WANTNUKEWARNING | NOCONFIRMATION
    op = SHFILEOPSTRUCTW(hwnd, 3, "\0".join(paths) + "\0", None, flags)  # FO_DELETE, 이중 NUL 종료
    ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return [p for p in paths if not os.path.lexists(p)], sizes


class Index:
    """파일명 검색 인덱스. 저장·검색·USN 반영은 C 확장(_fastindex.Names, 드라이브마다 하나)이 한다.
    관리자 + NTFS면 MFT로 만들고 USN 저널로 실시간 갱신, 아니면 폴더 스캔(변경은 '다시 인덱싱' 때 반영).
    ponytail: 지운 항목은 표시만 하고 자리는 남김 — 오래 켜 두면 조금씩 커짐, 다시 인덱싱하면 정리됨."""

    def __init__(self):
        self.vols = []  # _fastindex.Names 목록
        self.live = {}  # Names → {"vol", "jid", "usn"}

    def __len__(self):
        return sum(len(v) for v in self.vols)

    def add_drive(self, drive, stop=lambda: False):
        """관리자면 MFT로, 아니면(또는 NTFS가 아니면) 폴더를 직접 훑는다. 사용한 방식을 반환."""
        if is_admin():
            try:
                vol = Volume(drive)
            except OSError:
                vol = None
            if vol:
                try:
                    jid, usn = vol.journal()  # MFT 읽기 '전' 위치부터 따라가야 그 사이 변경을 놓치지 않음
                except OSError:
                    jid = None  # 이 볼륨은 저널이 꺼져 있음
                names = _fastindex.Names(drive)
                self.vols.append(names)
                try:
                    for buf in mft_buffers(vol, stop):
                        names.apply(buf)
                except OSError:
                    vol.close()
                    self.vols.remove(names)
                else:
                    names.compact()
                    if jid is None:
                        vol.close()
                        return "MFT"
                    self.live[names] = {"vol": vol, "jid": jid, "usn": usn}
                    return "MFT+USN"
        self.add_tree(drive, stop)
        return "폴더 스캔"

    def add_tree(self, root, stop=lambda: False):
        """폴더 스캔: 항목마다 가짜 FRN(일련번호)을 붙여 MFT와 같은 구조로 넣는다."""
        names = _fastindex.Names(root)
        self.vols.append(names)
        stack, n = [(root, 0)], 0  # 0 = 루트(항목 아님)
        while stack and not stop():
            d, dfrn = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        n += 1
                        try:
                            is_dir = e.is_dir(follow_symlinks=False) and not e.is_junction()
                        except OSError:
                            is_dir = False
                        names.add(n, dfrn, e.name, is_dir)
                        if is_dir:
                            stack.append((e.path, n))
            except OSError:
                pass
        names.compact()

    def poll(self):
        """실시간 드라이브의 USN 저널에서 새 변경을 읽어 반영. 레코드 수 반환, 저널이 끊기면 OSError."""
        n = 0
        for names, v in self.live.items():
            while True:
                nxt, data = v["vol"].read_journal(v["jid"], v["usn"])
                n += names.apply(data, True)
                if nxt == v["usn"]:
                    break
                v["usn"] = nxt
        return n

    def close(self):
        for v in self.live.values():
            v["vol"].close()
        self.live.clear()

    def search(self, query, limit=1000):
        """공백 = AND, * ? = 이름 전체 와일드카드, \\ 포함 단어 = 전체 경로 대상.
        (일치 수, 앞쪽 limit개의 (Names, 번호)) 반환."""
        terms = [(t, int("*" in t or "?" in t) | (2 if os.sep in t else 0)) for t in query.lower().split()]
        if not terms:
            return 0, []
        terms.sort(key=lambda t: t[1] & 2)  # 이름 조건 먼저(빠름), 경로 조건은 걸러진 것만
        total, hits = 0, []
        for names in self.vols:
            n, ids = names.search(terms, limit - len(hits))
            total += n
            hits += [(names, i) for i in ids]
        return total, hits

    @staticmethod
    def path(hit):
        return hit[0].path(hit[1])

    @staticmethod
    def name(hit):
        return hit[0].name(hit[1])


def fixed_drives():
    if os.name != "nt":
        return ["/"]
    return [f"{d}:\\" for d in string.ascii_uppercase if ctypes.windll.kernel32.GetDriveTypeW(f"{d}:\\") == 3]


def tab_title(p):
    return os.path.basename(p.rstrip("\\/")) or p


# ---------------------------------------------------------------- UI

class Worker(QThread):
    """fn(worker)를 백그라운드에서 실행하고 결과를 done으로 보낸다."""
    done = Signal(object)
    progress = Signal(int)

    def __init__(self, fn):
        super().__init__()
        self.fn, self.stopped, self.count = fn, False, 0

    def tick(self):
        self.count += 1
        if self.count % 500 == 0:
            self.progress.emit(self.count)

    def run(self):
        self.done.emit(self.fn(self))

    def stop(self):
        self.stopped = True
        self.wait()


def delete_keys(view, paths):
    """탐색기와 같은 키: Del = 휴지통, Shift+Del = 즉시 삭제."""
    for keys, permanent in (("Del", False), ("Shift+Del", True)):
        sc = QShortcut(QKeySequence(keys), view)
        sc.setContext(Qt.WidgetShortcut)
        sc.activated.connect(lambda permanent=permanent: view.window().delete(paths(), permanent))


def path_menu(view, pos, paths, extra=()):
    """경로 목록용 공통 우클릭 메뉴."""
    if not paths:
        return
    p = paths[0]
    m = QMenu(view)
    for text, fn in extra:
        m.addAction(text, fn)
    m.addAction("열기", lambda: os.startfile(p))
    m.addAction("탐색기에서 위치 보기", lambda: subprocess.Popen(f'explorer /select,"{p}"'))
    m.addAction("경로 복사", lambda: QApplication.clipboard().setText("\n".join(paths)))
    m.addSeparator()
    m.addAction("삭제\tDel", lambda: view.window().delete(paths, False))
    m.addAction("영구 삭제\tShift+Del", lambda: view.window().delete(paths, True))
    m.exec(view.viewport().mapToGlobal(pos))


class BarDelegate(QStyledItemDelegate):
    """TreeSize처럼 상위 폴더 대비 비율을 막대로 그린다."""

    def paint(self, p, opt, idx):
        super().paint(p, opt, idx)
        frac = idx.data(UR) or 0.0
        r = opt.rect.adjusted(3, 4, -3, -4)
        p.fillRect(r.x(), r.y(), int(r.width() * frac), r.height(), QColor(66, 133, 244, 150))
        p.setPen(opt.palette.text().color())
        p.drawText(opt.rect, Qt.AlignCenter, f"{frac:.1%}")


class SizeModel(QAbstractItemModel):
    """스캔 결과(Node 트리)를 그대로 보여주는 모델. 폴더마다 Qt 객체를 만들지 않고, 보이는 줄만 그때그때 그린다."""
    HEAD = ("폴더", "크기", "비율", "파일 수")
    KEYS = (lambda n: n.name.lower(), lambda n: n.size, lambda n: n.size, lambda n: n.files)  # 비율 = 같은 부모 안 크기 순

    def __init__(self, parent):
        super().__init__(parent)
        self.root = None
        self.icon = QFileIconProvider().icon(QFileIconProvider.Folder)

    def set_root(self, node):
        self.beginResetModel()
        self.root = node
        self.endResetModel()

    def index_of(self, n, col=0):
        return self.createIndex(n.row, col, n)

    def index(self, row, col, parent=QModelIndex()):
        if not parent.isValid():
            return self.createIndex(0, col, self.root) if self.root and row == 0 else QModelIndex()
        kids = parent.internalPointer().children
        return self.createIndex(row, col, kids[row]) if 0 <= row < len(kids) else QModelIndex()

    def parent(self, index=QModelIndex()):
        if not index.isValid():
            return QModelIndex()
        p = index.internalPointer().parent
        return self.createIndex(p.row, 0, p) if p else QModelIndex()

    def rowCount(self, parent=QModelIndex()):
        if not parent.isValid():
            return 1 if self.root else 0
        return len(parent.internalPointer().children) if parent.column() == 0 else 0

    def columnCount(self, parent=QModelIndex()):
        return 4

    def headerData(self, section, orient, role=Qt.DisplayRole):
        if orient == Qt.Horizontal and role == Qt.DisplayRole:
            return self.HEAD[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        n, c = index.internalPointer(), index.column()
        if role == Qt.DisplayRole:
            return n.name if c == 0 else human(n.size) if c == 1 else f"{n.files:,}" if c == 3 else None
        if role == UR:  # 0열 = 경로, 2열 = 상위 폴더 대비 비율
            if c == 0:
                return n.path
            if c == 2:
                return n.size / n.parent.size if n.parent and n.parent.size else float(not n.parent)
        elif role == Qt.DecorationRole and c == 0:
            return self.icon
        elif role == Qt.TextAlignmentRole and c in (1, 3):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        elif role == Qt.ForegroundRole and n.denied:
            return QColor("gray")
        elif role == Qt.ToolTipRole and n.denied:
            return "접근 거부 — 크기에 포함되지 않음"
        return None

    def sort(self, col, order=Qt.AscendingOrder):
        if not self.root:
            return
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()  # 펼침·선택 상태 유지
        keep = [(i.internalPointer(), i.column()) for i in old]
        key, stack = self.KEYS[col], [self.root]
        while stack:
            n = stack.pop()
            n.children.sort(key=key, reverse=order == Qt.DescendingOrder)
            for r, child in enumerate(n.children):
                child.row = r
            stack.extend(n.children)
        self.changePersistentIndexList(old, [self.createIndex(n.row, c, n) for n, c in keep])
        self.layoutChanged.emit()

    def find(self, path):
        """경로 → Node (트리에 없으면 None). 루트에서 이름으로 내려간다."""
        if not self.root:
            return None
        try:
            rel = os.path.relpath(path, self.root.name)
        except ValueError:  # 다른 드라이브
            return None
        if rel == ".":
            return self.root
        if rel.startswith(".."):
            return None
        n = self.root
        for part in os.path.normcase(rel).split(os.sep):
            n = next((c for c in n.children if os.path.normcase(c.name) == part), None)
            if not n:
                return None
        return n

    def remove(self, n):
        p = n.parent
        self.beginRemoveRows(self.index_of(p), n.row, n.row)
        del p.children[n.row]
        for r in range(n.row, len(p.children)):
            p.children[r].row = r
        self.endRemoveRows()

    def refresh(self, n):
        """n의 크기·파일 수, 그리고 자식들의 비율을 다시 그린다."""
        self.dataChanged.emit(self.index_of(n, 1), self.index_of(n, 3))
        if n.children:
            self.dataChanged.emit(self.index_of(n.children[0], 2), self.index_of(n.children[-1], 2))


class Pane(QWidget):
    """창 하나: 왼쪽 = 중심 폴더부터 펼친 폴더 용량 트리, 오른쪽 = 선택한 폴더의 파일 목록."""
    rooted = Signal(str)

    def __init__(self, path, depth=DEFAULT_DEPTH):
        super().__init__()
        self.root, self.worker, self.folders = None, None, 0

        self.addr = QLineEdit()
        self.addr.returnPressed.connect(lambda: self.load(self.addr.text()))
        bar = QHBoxLayout()
        for text, tip, fn in (("↑", "상위 폴더", lambda: self.load(os.path.dirname(self.root))),
                              ("…", "폴더 선택", self.browse),
                              ("⟳", "다시 스캔", lambda: self.load(self.root))):
            b = QToolButton(text=text, toolTip=tip)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addWidget(self.addr, 1)
        self.depth = QSpinBox()
        self.depth.setRange(0, 99)
        self.depth.setValue(depth)
        self.depth.setPrefix("펼침 깊이 ")
        self.depth.valueChanged.connect(self.apply_depth)
        bar.addWidget(self.depth)

        self.sizes = SizeModel(self)
        self.tree = QTreeView()
        self.tree.setModel(self.sizes)
        self.tree.setUniformRowHeights(True)  # 줄 높이 계산을 줄마다 하지 않음(큰 트리 속도)
        self.tree.setSortingEnabled(True)
        self.tree.setItemDelegateForColumn(2, BarDelegate(self.tree))
        self.tree.setColumnWidth(0, 260)
        self.tree.selectionModel().currentChanged.connect(lambda cur, _: cur.isValid() and self.show_dir(self.node_path(cur)))
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.tree_menu)
        delete_keys(self.tree, self.tree_selection)

        self.model = QFileSystemModel(self)
        self.files = QTreeView()
        self.files.setModel(self.model)
        self.files.setSortingEnabled(True)
        self.files.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.files.setColumnWidth(0, 260)
        self.files.doubleClicked.connect(self.open_entry)
        self.files.setContextMenuPolicy(Qt.CustomContextMenu)
        self.files.customContextMenuRequested.connect(lambda pos: path_menu(self.files, pos, self.file_selection()))
        delete_keys(self.files, self.file_selection)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.tree)
        split.addWidget(self.files)
        split.setSizes([500, 400])
        self.info = QLabel()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addLayout(bar)
        lay.addWidget(split, 1)
        lay.addWidget(self.info)
        self.load(existing(path))

    @staticmethod
    def node_path(index):
        return index.internalPointer().path

    def tree_selection(self):
        cur = self.tree.currentIndex()
        return [self.node_path(cur)] if cur.isValid() else []

    def file_selection(self):
        return [os.path.normpath(self.model.filePath(i)) for i in self.files.selectionModel().selectedRows()]

    def browse(self):
        p = QFileDialog.getExistingDirectory(self, "중심 폴더 선택", self.root)
        if p:
            self.load(p)

    def load(self, path):
        path = path.strip().strip('"')
        if path.endswith(":"):
            path += "\\"
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            self.info.setText(f"폴더가 없습니다: {path}")
            return
        self.stop()
        self.root = path
        self.addr.setText(path)
        self.rooted.emit(path)
        self.sizes.set_root(None)
        self.show_dir(path)
        self.info.setText("스캔 중…")
        w = self.worker = Worker(lambda w: scan(path, lambda: w.stopped, w.tick))
        w.progress.connect(lambda n: self.info.setText(f"스캔 중… 폴더 {n:,}개"))
        w.done.connect(self.on_scanned)
        w.start()

    def on_scanned(self, node):
        if not self.worker or self.sender() is not self.worker:  # 취소된 이전 스캔의 결과는 버림
            return
        self.folders = self.worker.count
        self.sizes.set_root(node)
        self.tree.sortByColumn(1, Qt.DescendingOrder)
        self.apply_depth()
        self.tree.setCurrentIndex(self.sizes.index_of(node))
        self.update_info()

    def update_info(self):
        top = self.sizes.root
        if not top:
            return
        du = shutil.disk_usage(os.path.splitdrive(self.root)[0] + os.sep)  # 중심 폴더가 지워져도 드라이브 루트는 있음
        self.info.setText(f"폴더 {self.folders:,}개 · 파일 {top.files:,}개 · {human(top.size)}"
                          f"   |   드라이브 {human(du.used)} / {human(du.total)} 사용 ({du.used / du.total:.0%})")

    def on_deleted(self, deleted, sizes):
        """다른 창/탭/검색에서 지운 것까지 포함해, 트리에서 빼고 상위 폴더 크기·비율을 다시 계산."""
        if not self.root or not os.path.isdir(self.root):  # 중심 폴더 자체가 지워짐
            self.load(existing(self.root))
            return
        for p in deleted:
            n = self.sizes.find(p)
            if n and n.parent:  # 트리에 있는 폴더
                parent, size, files = n.parent, n.size, n.files
                stack = [n]
                while stack:  # 지운 폴더 수만큼 폴더 개수에서 뺀다
                    self.folders -= 1
                    stack.extend(stack.pop().children)
                self.sizes.remove(n)
            else:  # 파일 (또는 트리에 없는 폴더)
                parent = self.sizes.find(os.path.dirname(p))
                size, files = sizes.get(p, 0), 1
            while parent:
                parent.size -= size
                parent.files -= files
                self.sizes.refresh(parent)
                parent = parent.parent
        self.update_info()

    def apply_depth(self):
        self.tree.collapseAll()
        self.tree.expandToDepth(self.depth.value())

    def show_dir(self, path):
        self.files.setRootIndex(self.model.setRootPath(path))

    def open_entry(self, index):
        p = os.path.normpath(self.model.filePath(index))
        if not self.model.isDir(index):
            os.startfile(p)
        elif n := self.sizes.find(p):
            self.tree.setCurrentIndex(self.sizes.index_of(n))
        else:
            self.show_dir(p)

    def tree_menu(self, pos):
        idx = self.tree.indexAt(pos)
        if idx.isValid():
            p = self.node_path(idx)
            path_menu(self.tree, pos, [p], (("이 폴더를 중심으로", lambda: self.load(p)),
                                            ("새 탭에서 열기", lambda: self.window().open_tab(p))))

    def stop(self):
        if self.worker:
            self.worker.stop()
            self.worker = None


class Workspace(QSplitter):
    """탭 하나 = 최대 2x2 분할 창. state = {"layout": [행, 열], "panes": {"행,열": [폴더, 깊이]}}"""
    rooted = Signal(str)
    LAYOUTS = (("1 창", "Ctrl+1", 1, 1), ("2 좌우", "Ctrl+2", 1, 2), ("2 상하", "Ctrl+3", 2, 1), ("4 창", "Ctrl+4", 2, 2))

    def __init__(self, state):
        super().__init__(Qt.Vertical)
        self.rows = [QSplitter(Qt.Horizontal) for _ in range(2)]
        for r in self.rows:
            self.addWidget(r)
        self.panes, self.saved = {}, state["panes"]
        self.set_layout(*state["layout"])

    @property
    def main(self):
        return self.panes["0,0"]

    def pane(self, r, c):
        key = f"{r},{c}"
        if key not in self.panes:
            root, depth = self.saved.get(key) or (self.main.root, self.main.depth.value())
            p = Pane(root, depth)
            if key == "0,0":
                p.rooted.connect(self.rooted)
            self.rows[r].addWidget(p)
            self.panes[key] = p
        return self.panes[key]

    def set_layout(self, rows, cols):
        self.shape = [rows, cols]
        for r in range(2):
            self.rows[r].setVisible(r < rows)
            for c in range(2):
                if r < rows and c < cols:
                    self.pane(r, c).show()
                elif f"{r},{c}" in self.panes:
                    self.panes[f"{r},{c}"].hide()

    def state(self):
        return {"layout": self.shape, "panes": {k: [p.root, p.depth.value()] for k, p in self.panes.items()}}

    def stop(self):
        for p in self.panes.values():
            p.stop()


class SearchPanel(QWidget):
    """Everything 스타일: 모든 고정 드라이브를 인덱싱하고 입력하는 즉시 검색."""
    open_folder = Signal(str)

    def __init__(self):
        super().__init__()
        self.index, self.worker = Index(), None
        self.box = QLineEdit(placeholderText="파일 이름 검색 — 공백 = AND,  *.pdf,  \\경로\\일부")
        self.box.setClearButtonEnabled(True)
        self.status = QLabel()
        again = QToolButton(text="다시 인덱싱")
        again.clicked.connect(self.build)
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["이름", "폴더", "크기"])
        self.list.setColumnWidth(0, 280)
        self.list.setColumnWidth(1, 520)
        self.list.setRootIsDecorated(False)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.itemDoubleClicked.connect(lambda it: os.startfile(it.data(0, UR)))
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.menu)
        delete_keys(self.list, self.selection)

        self.debounce = QTimer(self, singleShot=True, interval=200, timeout=self.run)
        self.box.textChanged.connect(self.debounce.start)
        self.poll = QTimer(self, interval=500, timeout=lambda: self.status.setText(f"인덱싱 중… {len(self.index):,}개"))
        self.live = QTimer(self, interval=1000, timeout=self.follow_journal)

        top = QHBoxLayout()
        top.addWidget(self.box, 1)
        top.addWidget(self.status)
        top.addWidget(again)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addLayout(top)
        lay.addWidget(self.list)
        self.build()

    def selection(self):
        return [it.data(0, UR) for it in self.list.selectedItems()]

    def build(self):
        self.stop()
        idx = self.index = Index()
        drives = fixed_drives()
        t0 = time.monotonic()
        self.worker = Worker(lambda w: ([idx.add_drive(d, lambda: w.stopped) for d in drives], time.monotonic() - t0))
        self.worker.done.connect(self.indexed)
        self.worker.start()
        self.poll.start()

    def indexed(self, result):
        if not self.worker or self.sender() is not self.worker:  # 취소된 이전 인덱싱
            return
        modes, secs = result
        self.poll.stop()
        if self.index.live:
            hint = " · USN 실시간 반영 중"
            self.live.start()
        elif is_admin():
            hint = ""
        else:
            hint = " · 관리자로 실행하면 MFT로 수 초 만에 인덱싱 + 실시간 반영"
        self.status.setText(f"{len(self.index):,}개 인덱싱 ({secs:.1f}초, {'/'.join(sorted(set(modes)))}){hint}")
        self.run()

    def follow_journal(self):
        try:
            changed = self.index.poll()
        except OSError:  # 저널이 덮어써졌거나 재생성됨 → 처음부터
            self.status.setText("USN 저널이 끊겨 다시 인덱싱합니다…")
            self.build()
            return
        if changed and self.box.text().strip():
            self.run()

    def run(self):
        total, hits = self.index.search(self.box.text())
        self.list.clear()
        items = []
        for i in hits:
            p = self.index.path(i)
            try:
                st = os.stat(p)
            except OSError:
                continue  # 인덱싱 후 지워진 항목
            it = QTreeWidgetItem([self.index.name(i), os.path.dirname(p), "" if os.path.isdir(p) else human(st.st_size)])
            it.setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)
            it.setData(0, UR, p)
            items.append(it)
        self.list.addTopLevelItems(items)
        if self.box.text().strip():
            more = f" (상위 {len(hits):,}개 표시)" if total > len(hits) else ""
            self.status.setText(f"{total:,}개 일치{more} · 인덱스 {len(self.index):,}개")

    def menu(self, pos):
        paths = self.selection()
        if paths:
            p = paths[0]
            folder = p if os.path.isdir(p) else os.path.dirname(p)
            path_menu(self.list, pos, paths, (("새 탭에서 폴더 열기", lambda: self.open_folder.emit(folder)),))

    def stop(self):
        if self.worker:
            self.worker.stop()
            self.worker = None
        self.poll.stop()
        self.live.stop()
        self.index.close()


class Main(QMainWindow):
    def __init__(self, path=None):
        super().__init__()
        self.settings = QSettings("DirTree", "DirTree")
        self.setWindowTitle("DirTree [관리자]" if is_admin() else "DirTree")
        self.tabs = QTabWidget(documentMode=True, tabsClosable=True, movable=True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.setCentralWidget(self.tabs)

        tb = self.addToolBar("도구")

        def act(text, keys, fn):
            tb.addAction(text, fn).setShortcut(QKeySequence(keys))

        act("새 탭", "Ctrl+T", lambda: self.open_tab(self.tabs.currentWidget().main.root))
        act("탭 닫기", "Ctrl+W", lambda: self.close_tab(self.tabs.currentIndex()))
        tb.addSeparator()
        for text, keys, r, c in Workspace.LAYOUTS:
            act(text, keys, lambda r=r, c=c: self.tabs.currentWidget().set_layout(r, c))
        tb.addSeparator()

        self.search = SearchPanel()
        self.search.open_folder.connect(self.open_tab)
        dock = QDockWidget("검색", self)
        dock.setWidget(self.search)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)

        def focus_search():
            dock.show()
            self.search.box.setFocus()
            self.search.box.selectAll()

        act("검색", "Ctrl+F", focus_search)
        if not is_admin():
            tb.addSeparator()
            act("관리자 권한으로 실행", "", self.elevate)

        self.resize(1400, 860)
        self.restore(path)

    # --- 탭 기억
    def restore(self, path):
        try:
            state = json.loads(self.settings.value("tabs") or "{}")
            for s in state.get("tabs", []):
                self.add_workspace(s)
            self.tabs.setCurrentIndex(state.get("current", 0))
        except (ValueError, KeyError, TypeError):
            pass  # 저장 상태가 깨졌으면 새로 시작
        if path or not self.tabs.count():
            self.open_tab(path or HOME)
        if geo := self.settings.value("geometry"):
            self.restoreGeometry(geo)

    def save_state(self):
        tabs = [self.tabs.widget(i).state() for i in range(self.tabs.count())]
        self.settings.setValue("tabs", json.dumps({"tabs": tabs, "current": self.tabs.currentIndex()}))
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.sync()

    def add_workspace(self, state):
        ws = Workspace(state)
        self.tabs.setCurrentIndex(self.tabs.addTab(ws, tab_title(ws.main.root)))
        ws.rooted.connect(lambda p: self.tabs.setTabText(self.tabs.indexOf(ws), tab_title(p)))

    def open_tab(self, path):
        self.add_workspace({"layout": [1, 1], "panes": {"0,0": [path, DEFAULT_DEPTH]}})

    def close_tab(self, i):
        if self.tabs.count() > 1:
            ws = self.tabs.widget(i)
            ws.stop()
            self.tabs.removeTab(i)
            ws.deleteLater()

    # --- 삭제
    def delete(self, paths, permanent):
        if not paths:
            return
        deleted, sizes = delete_paths(paths, permanent, int(self.winId()))
        if deleted:
            for i in range(self.tabs.count()):
                for p in self.tabs.widget(i).panes.values():
                    p.on_deleted(deleted, sizes)
            self.search.run()

    # --- 관리자 권한
    def elevate(self):
        self.save_state()
        if relaunch_as_admin():
            self.close()

    def closeEvent(self, e):
        self.save_state()
        for i in range(self.tabs.count()):
            self.tabs.widget(i).stop()
        self.search.stop()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    w = Main(sys.argv[1] if len(sys.argv) > 1 else None)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
