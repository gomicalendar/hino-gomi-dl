#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日野市「ごみ・資源分別カレンダー」PDF を配布ページからダウンロードする。

    python hino_gomi_dl.py
    → data/r8/r8.toyoda.pdf … と data/r8/manifest.json を作る
    → そのまま hino-gomi-py の入力にできる:
      python ../hino-gomi-py/hino_gomi.py data/r8 -o out

配布ページの HTML から PDF リンクを見つけて取得する。ファイル名や年度の接頭辞は
決め打ちしない（令和8年 = r8.*、翌年 r9.* になっても無改修）。複数年度のリンクが
同時に並んでいる場合は最も新しい年度だけを取り、年度ごとのフォルダに分けて置く。

1 ファイル約 26MB × 10 地区あるため、無駄な再取得と取り直しを避ける:

- 保存先にファイルがあれば、それだけで取得済みと判断して通信しない。
  配布ページの取得（HTML 数十 KB）以外、一切の転送が発生しない。
- 差し替えを拾いたいときは --refresh。ETag / Last-Modified を manifest.json に
  記録してあるので条件付き GET で問い合わせ、304 ならダウンロードしない。
- 中断したら Range で途中から再開する（配信元は Accept-Ranges: bytes に対応）。
- 落とし終えるまで .part に書き、検証してから正式名にリネームする。
  途中で失敗したファイルを hino-gomi-py に食わせてしまうことがない。

標準ライブラリだけで動く（追加パッケージ不要、Python 3.10 以上）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

PAGE_URL = "https://www.city.hino.lg.jp/kurashi/gomi/kihon/1002863.html"
USER_AGENT = "hino-gomi-dl/1.0 (Python urllib)"
MANIFEST_NAME = "manifest.json"

# 配布ファイル名は「<元号><年>.<地区>.pdf」。r8 = 令和8年。
# 元号の字数は決め打ちしない（r9 / a10 / bb11 / cx12 いずれも拾う）。
# 数字だけの命名（2026.toyoda.pdf）にも備えて、元号部分は 0 文字でも通す。
PDF_NAME_RE = re.compile(r"([a-z]*)(\d+)\.([0-9a-z_]+)\.pdf", re.IGNORECASE)

# 全地区共通ページ。カレンダー表ではないので hino-gomi-py の入力には含めない。
COMMON_AREA = "common"

# 例年の地区数。増減したら体裁変更の可能性があるので警告する（処理は続ける）。
EXPECTED_AREAS = 10

CHUNK = 256 * 1024
RETRIABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# 配布ページの解析
# ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    """全角空白や連続空白を畳み、リンク末尾の「(PDF 25.7MB)」を落とす。"""
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()
    return re.sub(r"\s*\(\s*PDF[^)]*\)\s*$", "", text, flags=re.IGNORECASE).strip()


class LinkCollector(HTMLParser):
    """<a href> と、そのリンク文字列を集める。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        self._flush()                       # 入れ子の <a>（不正な HTML）でも取りこぼさない
        for key, value in attrs:
            if key.lower() == "href" and value:
                self._href = value

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def close(self) -> None:
        super().close()
        self._flush()

    def _flush(self) -> None:
        if self._href is not None:
            self.links.append((self._href, normalize("".join(self._text))))
        self._href = None
        self._text = []


@dataclass(frozen=True)
class Target:
    url: str
    name: str                               # r8.toyoda.pdf
    era: str                                # r（令和）。数字だけの命名なら空
    number: int                             # 8
    area: str                               # toyoda
    label: str                              # リンク文字列（令和8年版 多摩平 など）

    @property
    def edition(self) -> str:
        """r8 / a10 / 2026 など。保存先フォルダ名にも使う。"""
        return f"{self.era}{self.number}"

    @property
    def is_common(self) -> bool:
        return self.area == COMMON_AREA


def find_targets(page_url: str, html: str,
                 wanted_edition: str | None = None) -> tuple[list[Target], str, int]:
    """PDF リンクを集め、1 年度ぶんだけ返す。

    戻り値は (対象, 年度, 除いた他年度の件数)。
    """
    collector = LinkCollector()
    collector.feed(html)
    collector.close()

    found: dict[str, Target] = {}
    for href, label in collector.links:
        url = urllib.parse.urljoin(page_url, href)
        parts = urllib.parse.urlsplit(url)._replace(query="", fragment="")
        url = urllib.parse.urlunsplit(parts)
        name = urllib.parse.unquote(parts.path.rsplit("/", 1)[-1]).lower()
        matched = PDF_NAME_RE.fullmatch(name)
        if not matched:
            continue
        target = Target(url, name, matched.group(1).lower(),
                        int(matched.group(2)), matched.group(3), label)
        # 同じ PDF が複数箇所からリンクされていても 1 回だけ落とす
        found.setdefault(name, target)

    if not found:
        raise LookupError("PDF リンクが 1 つも見つかりません（ページの体裁が変わった可能性）")

    editions = sorted({target.edition for target in found.values()})
    if wanted_edition:
        edition = wanted_edition
        if edition not in editions:
            raise LookupError(f"年度 {edition} のリンクがありません（ある年度: {', '.join(editions)}）")
    elif len({target.era for target in found.values()}) > 1:
        # 元号が変わると番号は 1 に戻るため、数字の大小では新旧を決められない
        raise LookupError(f"元号の異なるリンクが混在しています（{', '.join(editions)}）。"
                          "--edition でどれを取るか指定してください")
    else:
        number = max(target.number for target in found.values())
        edition = next(t.edition for t in found.values() if t.number == number)

    targets = [target for target in found.values() if target.edition == edition]
    targets.sort(key=lambda t: (not t.is_common, t.area))
    return targets, edition, len(found) - len(targets)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def open_url(url: str, headers: dict[str, str], timeout: float, retries: int,
             allow_status: tuple[int, ...] = ()):
    """再試行付きで開く。allow_status の応答は例外にせずそのまま返す。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    delay = 2.0
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in allow_status:
                return exc
            exc.close()
            if exc.code not in RETRIABLE_STATUS or attempt == retries:
                raise
            last = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            if attempt == retries:
                raise
            last = exc
        time.sleep(delay)
        delay *= 2
    raise last or RuntimeError("再試行に失敗しました")


def status_of(response) -> int:
    return getattr(response, "status", None) or getattr(response, "code", 0)


def fetch_text(url: str, timeout: float, retries: int) -> str:
    with open_url(url, {}, timeout, retries) as response:
        raw = response.read()
        charset = response.headers.get_content_charset()
    for encoding in (charset, "utf-8", "cp932"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# ダウンロード
# ---------------------------------------------------------------------------


def human(size: float) -> str:
    return f"{size / 1_000_000:.1f}MB"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().replace(microsecond=0).isoformat()


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify_pdf(path: Path) -> None:
    """PDF らしさを見る。HTML のエラーページや切れたファイルを通さない。"""
    size = path.stat().st_size
    with path.open("rb") as handle:
        head = handle.read(5)
        handle.seek(max(0, size - 4096))
        tail = handle.read()
    if head != b"%PDF-":
        raise ValueError("PDF ではありません（HTML のエラーページの可能性）")
    if b"%%EOF" not in tail:
        raise ValueError("終端の %%EOF がありません（壊れている可能性）")


def progress_reporter(name: str, enabled: bool):
    if not enabled:
        return lambda done, total, final=False: None

    state = {"at": 0.0, "width": 0}

    def report(done: int, total: int | None, final: bool = False) -> None:
        now = time.monotonic()
        if not final and now - state["at"] < 0.2:
            return
        state["at"] = now
        if total:
            line = f"  {name}  {human(done)}/{human(total)} ({done * 100 // total}%)"
        else:
            line = f"  {name}  {human(done)}"
        state["width"] = max(state["width"], len(line))
        if final:
            line = ""
        print(f"\r{line.ljust(state['width'])}", end="" if not final else "\r",
              file=sys.stderr, flush=True)

    return report


def conditional_headers(target: Target, path: Path, entry: dict) -> dict[str, str]:
    """既にあるファイルを落とし直さないための問い合わせヘッダ。"""
    if entry.get("url") == target.url:
        if entry.get("etag"):
            return {"If-None-Match": entry["etag"]}
        if entry.get("last_modified"):
            return {"If-Modified-Since": entry["last_modified"]}
    # manifest が無い（手で置いた等）場合はファイルの更新時刻で問い合わせる
    return {"If-Modified-Since": email.utils.formatdate(path.stat().st_mtime, usegmt=True)}


def resume_headers(target: Target, part: Path, meta: Path) -> tuple[dict[str, str], int]:
    """前回の中断ぶんを引き継ぐ Range ヘッダ。引き継げなければ空。"""
    saved = read_json(meta)
    validator = saved.get("etag") or saved.get("last_modified")
    offset = part.stat().st_size
    if saved.get("url") != target.url or not validator or offset <= 0:
        return {}, 0
    # If-Range: 中身が入れ替わっていたらサーバは 206 ではなく 200 を返す → 最初から取り直す
    return {"Range": f"bytes={offset}-", "If-Range": validator}, offset


def download(target: Target, outdir: Path, entry: dict, args) -> tuple[str, dict]:
    """1 ファイル取得する。戻り値は (new|updated|skip|have, manifest エントリ)。"""
    path = outdir / target.name
    part = outdir / f"{target.name}.part"
    meta = outdir / f"{target.name}.part.json"
    existed = path.exists()

    # 保存先にあるなら取得済み。サーバに問い合わせもしない（--refresh で確認する）
    if existed and not (args.force or args.refresh):
        return "have", entry

    headers: dict[str, str] = {}
    offset = 0
    if not args.force:
        if existed:
            headers = conditional_headers(target, path, entry)
        elif part.exists() and meta.exists():
            headers, offset = resume_headers(target, part, meta)

    response = open_url(target.url, headers, args.timeout, args.retries, allow_status=(304,))
    try:
        code = status_of(response)
        if code == 304:
            return "skip", entry
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")

        if code == 206:
            ranged = re.search(r"/(\d+)\s*$", response.headers.get("Content-Range", ""))
            total = int(ranged.group(1)) if ranged else None
            mode = "ab"
        else:
            offset, mode = 0, "wb"          # 206 以外は全体が返ってきている
            length = response.headers.get("Content-Length", "")
            total = int(length) if length.isdigit() else None

        # 途中で落ちても次回 Range で再開できるよう、書き始める前に検証子を残す
        write_json(meta, {"url": target.url, "etag": etag,
                          "last_modified": last_modified, "total": total})

        digest = hashlib.sha256()
        if mode == "ab":
            with part.open("rb") as handle:
                for block in iter(lambda: handle.read(CHUNK), b""):
                    digest.update(block)

        report = progress_reporter(target.name, args.progress)
        done = offset
        with part.open(mode) as handle:
            while True:
                block = response.read(CHUNK)
                if not block:
                    break
                handle.write(block)
                digest.update(block)
                done += len(block)
                report(done, total)
        report(done, total, final=True)
    finally:
        response.close()

    size = part.stat().st_size
    if total is not None and size != total:
        # .part は残す。もう一度実行すれば続きから取りに行く
        raise ValueError(f"転送が途中で切れました（{size}/{total} バイト）。"
                         "もう一度実行すると続きから再開します")

    try:
        verify_pdf(part)
    except ValueError:
        part.unlink(missing_ok=True)        # 中身が別物なので引き継がない
        meta.unlink(missing_ok=True)
        raise

    os.replace(part, path)
    meta.unlink(missing_ok=True)
    return "updated" if existed else "new", {
        "url": target.url,
        "area": target.area,
        "label": target.label,
        "bytes": size,
        "sha256": digest.hexdigest(),
        "etag": etag,
        "last_modified": last_modified,
        "downloaded_at": now_iso(),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="日野市ごみ・資源分別カレンダー PDF をダウンロードする")
    parser.add_argument("-o", "--outdir", default="data",
                        help="保存先の親ディレクトリ。配下に年度フォルダ（r8 など）を作る"
                             "（既定: data）")
    parser.add_argument("--url", default=PAGE_URL, help="配布ページの URL")
    parser.add_argument("--edition", metavar="ID",
                        help="取得する年度を指定する（例: --edition r8）。"
                             "既定は配布ページにある最新年度")
    parser.add_argument("--include-common",
                        action="store_true",
                        help="全地区共通ページ（r*.common.pdf）も保存する。"
                             "カレンダー表ではないため既定では除く")
    parser.add_argument("--area", action="append", metavar="ID",
                        help="地区を絞る（例: --area toyoda --area mogusa）。"
                             "既定は全地区")
    parser.add_argument("--refresh", action="store_true",
                        help="保存済みのファイルもサーバに変更を問い合わせる"
                             "（ETag / Last-Modified。差し替えを拾いたいとき）")
    parser.add_argument("--force", action="store_true",
                        help="問い合わせもせず、すべて取得し直す")
    parser.add_argument("--list", action="store_true",
                        help="取得せず、対象の一覧だけ表示する")
    parser.add_argument("--timeout", type=float, default=30.0, help="通信のタイムアウト秒（既定: 30）")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="ファイル間の待ち秒。配信元に連続で叩き込まないため（既定: 1）")
    parser.add_argument("--retries", type=int, default=3, help="再試行回数（既定: 3）")
    parser.add_argument("--quiet", action="store_true", help="進捗表示を出さない")
    args = parser.parse_args(argv)
    args.progress = not args.quiet and sys.stderr.isatty()

    try:
        html = fetch_text(args.url, args.timeout, args.retries)
        targets, edition, old = find_targets(
            args.url, html, args.edition.lower().strip() if args.edition else None)
    except Exception as exc:
        print(f"[NG] 配布ページを読めません: {exc}", file=sys.stderr)
        return 1

    if old:
        print(f"他年度のリンク {old} 件は無視しました（採用: {edition}）")
    areas = [t for t in targets if not t.is_common]
    if len(areas) != EXPECTED_AREAS:
        print(f"[!] 地区別 PDF が {len(areas)} 件です（例年 {EXPECTED_AREAS} 件）。"
              "ページの体裁が変わった可能性があります", file=sys.stderr)
    if not args.include_common:
        targets = areas
    if args.area:
        wanted = {area.lower() for area in args.area}
        unknown = wanted - {t.area for t in targets}
        if unknown:
            hint = ""
            if COMMON_AREA in unknown and not args.include_common:
                hint = f"（{COMMON_AREA} は --include-common と併せて指定してください）"
            print(f"[NG] 該当しない地区: {', '.join(sorted(unknown))}{hint}", file=sys.stderr)
            return 1
        targets = [t for t in targets if t.area in wanted]
    if not targets:
        print("取得対象がありません", file=sys.stderr)
        return 1

    # 年度ごとにフォルダを分ける。翌年ぶんは data/r9 に入り、前年ぶんはそのまま残る
    outdir = Path(args.outdir) / edition

    if args.list:
        for target in targets:
            mark = "済" if (outdir / target.name).exists() else "未"
            print(f"[{mark}] {outdir / target.name}  {target.url}  [{target.label}]")
        have = sum(1 for t in targets if (outdir / t.name).exists())
        print(f"計 {len(targets)} 件（{edition}）: 取得済み {have} / 未取得 {len(targets) - have}")
        return 0

    outdir.mkdir(parents=True, exist_ok=True)
    manifest = read_json(outdir / MANIFEST_NAME)
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    files = dict(files)

    counts = {"new": 0, "updated": 0, "skip": 0, "have": 0}
    failures = 0
    requested = 0                           # 実際にサーバへ問い合わせた件数

    for index, target in enumerate(targets):
        already = (outdir / target.name).exists() and not (args.force or args.refresh)
        if requested and not already and args.delay > 0:
            time.sleep(args.delay)          # 通信するときだけ間隔を空ける
        try:
            status, entry = download(target, outdir, files.get(target.name, {}), args)
        except KeyboardInterrupt:
            print("\n中断しました。もう一度実行すると続きから再開します", file=sys.stderr)
            return 130
        except Exception as exc:
            # 1 本失敗したら残りは取りに行かない（全キャンセル）
            failures += 1
            remaining = len(targets) - index - 1
            print(f"[NG] {target.name}: {exc}", file=sys.stderr)
            print(f"中止しました。残り {remaining} 件は取得していません。"
                  "取得を終えたファイルはそのまま残すので、"
                  "もう一度実行すると未取得ぶんだけ取りに行きます", file=sys.stderr)
            break

        if not already:
            requested += 1
        counts[status] += 1
        if status == "have":
            print(f"[--] {target.name}  取得済み")
        elif status == "skip":
            print(f"[--] {target.name}  変更なし")
        else:
            files[target.name] = entry
            note = "新規" if status == "new" else "更新"
            print(f"[OK] {target.name} -> {outdir / target.name}  "
                  f"{human(entry['bytes'])} {note}  [{target.label}]")

            # 1 件ごとに書き出す。途中で止まってもここまでの記録は残る
            write_json(outdir / MANIFEST_NAME, {
                "source": {"page": args.url, "fetched_at": now_iso()},
                "edition": edition,
                "files": {name: files[name] for name in sorted(files)
                          if (outdir / name).exists()},
            })

    labels = [("new", "新規"), ("updated", "更新"),
              ("skip", "変更なし"), ("have", "取得済み")]
    summary = [f"{label} {counts[key]}" for key, label in labels if counts[key]]
    print(" / ".join([*summary, f"失敗 {failures}"]) + f" -> {outdir}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
