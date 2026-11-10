import argparse
import csv
import io
import json
import re
import tempfile
import zipfile
from collections import Counter
from pathlib import Path
from statistics import median
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from fontTools.pens.basePen import BasePen
from fontTools.pens.boundsPen import BoundsPen

BASE = Path(__file__).resolve().parent


def read_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def load_font(match):
    family = match["Family directory"]
    path = BASE / "fonts" / family
    notice = (path / match["License file"]).read_bytes()
    if b"SIL OPEN FONT LICENSE" not in notice:
        raise ValueError(f"Missing SIL Open Font License in {path}")
    data = (path / match["Font file"]).read_bytes()
    weight = int(match["Weight"]) if match["Weight"] else None
    font = TTFont(io.BytesIO(data), recalcTimestamp=False)
    if weight is not None:
        font = instantiateVariableFont(font, {"wght": weight}, updateFontNames=False)
        name = f"Storygramo {family.title()} {weight}"
        table = font["name"]
        names = {
            1: name,
            2: "Regular",
            3: name + " 1.0",
            4: name,
            6: name.replace(" ", ""),
            16: name,
            17: "Regular",
        }
        for record in table.names[:]:
            if record.nameID in (1, 2, 3, 4, 6, 16, 17, 18, 20, 21, 22, 25):
                table.removeNames(
                    record.nameID, record.platformID, record.platEncID, record.langID
                )
        for key, value in names.items():
            table.setName(value, key, 3, 1, 1033)
            table.setName(value, key, 1, 0, 0)
        # Keep copyright and license records from the upstream font.
        stream = io.BytesIO()
        font.save(stream)
        data = stream.getvalue()
        font = TTFont(io.BytesIO(data), recalcTimestamp=False)
    return family, notice, data, font


class LottiePen(BasePen):
    def __init__(self, glyph_set, sx, sy):
        super().__init__(glyph_set)
        self.sx = sx
        self.sy = sy
        self.contours = []

    def point(self, p):
        return [round(p[0] * self.sx, 4), round(-p[1] * self.sy, 4)]

    def _moveTo(self, p):
        self.v = [self.point(p)]
        self.i = [[0, 0]]
        self.o = [[0, 0]]

    def _lineTo(self, p):
        self.v.append(self.point(p))
        self.i.append([0, 0])
        self.o.append([0, 0])

    def _curveToOne(self, a, b, p):
        p, a, b = map(self.point, (p, a, b))
        self.o[-1] = [round(x - y, 4) for x, y in zip(a, self.v[-1])]
        self.v.append(p)
        self.i.append([round(x - y, 4) for x, y in zip(b, p)])
        self.o.append([0, 0])

    def _closePath(self):
        if len(self.v) > 1 and self.v[-1] == self.v[0]:
            self.i[0] = self.i.pop()
            self.v.pop()
            self.o.pop()
        if len(self.v) > 1:
            self.contours.append({"i": self.i, "o": self.o, "v": self.v, "c": True})

    def _endPath(self):
        self._closePath()


def glyph_name(font, ch):
    assert len(ch) == 1, (ch, len(ch))
    name = font.getBestCmap().get(ord(ch))
    if name is None and ch in ("\r", "\n", "\u0003"):
        return None
    if name is None and ch in ("\u00a0", "\u200b"):
        return font.getBestCmap().get(32)
    assert name is not None, (font["name"].getDebugName(4), repr(ch))
    return name


def old_top(g):
    points = [
        p
        for s in g["data"].get("shapes", [])
        for q in s.get("it", [])
        if q.get("ty") == "sh"
        for p in q["ks"]["k"]["v"]
    ]
    return -min((p[1] for p in points), default=0)


def update_glyphs(document, loaded):
    by_face = {
        (f["fFamily"], f["fStyle"]): f["fName"] for f in document["fonts"]["list"]
    }
    count = 0
    for face, alias in by_face.items():
        if alias not in loaded:
            continue
        font = loaded[alias][3]
        glyphset = font.getGlyphSet()
        unit = 100 / font["head"].unitsPerEm
        glyphs = [g for g in document["chars"] if (g["fFamily"], g["style"]) == face]
        sample = [(g, glyph_name(font, g["ch"])) for g in glyphs]
        widths = []
        heights = []
        for g, n in sample:
            if not n or not g["ch"].isalnum():
                continue
            b = BoundsPen(glyphset)
            glyphset[n].draw(b)
            if g["w"] > 0 and glyphset[n].width > 0:
                widths.append(g["w"] / (glyphset[n].width * unit))
            if g["ch"].isupper() and b.bounds and b.bounds[3] > 0 and old_top(g) > 0:
                heights.append(old_top(g) / (b.bounds[3] * unit))
        # Use the original title's proportions to preserve its composition.
        sx = unit * min(1.4, max(0.7, median(widths))) if widths else unit
        sy = unit * min(1.4, max(0.7, median(heights))) if heights else unit
        for g, n in sample:
            pen = LottiePen(glyphset, sx, sy)
            if n:
                glyphset[n].draw(pen)
            items = [
                {"ty": "sh", "ind": i, "ix": i + 1, "ks": {"a": 0, "k": curve, "ix": 2}}
                for i, curve in enumerate(pen.contours)
            ]
            if items:
                g.setdefault("data", {})["shapes"] = [{"ty": "gr", "it": items}]
            else:
                g.pop("data", None)
            g["w"] = round(glyphset[n].width * sx, 4) if n else 0
            count += 1
    return count


def build(source, output, audit, choices):
    cache = {}
    totals = Counter()
    configs = sorted(source.rglob("template.json"))
    if not configs:
        raise ValueError(f"No template.json files found under {source}")
    for config in configs:
        root = config.parent
        relative = root.relative_to(source)
        category = relative.parts[0] if relative.parts else root.name
        res = root / "res"
        font_dir = next(
            (d for d in (res / "fonts", res / "font") if d.exists()), res / "fonts"
        )
        old_fonts = {
            p.stem: p
            for p in font_dir.glob("*")
            if p.suffix.lower() in (".ttf", ".otf")
        }
        data_path = res / "data.json"
        html_path = res / "data.html"
        data = json.loads(data_path.read_text(encoding="utf-8-sig"))
        html = html_path.read_text(encoding="utf-8-sig")
        match = re.search(r"(?:var|let|const)\s+animationData\s*=\s*(\{)", html)
        if not match:
            raise ValueError(f"No embedded animation data in {html_path}")
        start = match.start(1)
        html_data, length = json.JSONDecoder().raw_decode(html, start)
        if html_data != data:
            raise ValueError(f"HTML animation data differs from data.json in {root}")
        refs = {f["fName"] for f in data["fonts"]["list"]}
        bad = {
            n
            for n, p in old_fonts.items()
            if not audit.get(p.name.casefold(), "").startswith("Free / Open")
        }
        missing = refs - old_fonts.keys()
        aliases = bad | missing
        if not aliases:
            print("Excluded", category)
            continue
        loaded = {}
        for alias in sorted(aliases):
            original_name = (
                old_fonts[alias].name if alias in old_fonts else alias + ".ttf"
            )
            match_key = (
                original_name.casefold()
                if original_name.casefold() in choices
                else alias.casefold()
            )
            if match_key not in choices:
                raise ValueError(
                    f"{category}: add an approved match for {original_name} to the font matches CSV"
                )
            if match_key not in cache:
                cache[match_key] = load_font(choices[match_key])
            loaded[alias] = cache[match_key]
        changed = update_glyphs(data, loaded)
        encoded = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        files = {
            p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*")
            if p.is_file()
        }
        files["res/data.json"] = encoded.encode("utf-8")
        files["res/data.html"] = (html[:start] + encoded + html[length:]).encode(
            "utf-8"
        )
        paths = []
        for alias in sorted(aliases):
            family, notice, content, font = loaded[alias]
            name = old_fonts[alias].name if alias in old_fonts else alias + ".ttf"
            dest = "res/" + font_dir.name + "/" + name
            files[dest] = content
            files["res/font-licenses/" + family + "-OFL.txt"] = notice
            paths.append(dest)
            totals["replacements"] += 1
        assert set(files) >= set(paths)
        folder = output / category
        folder.mkdir(exist_ok=True)
        archive = folder / (root.name + ".zip")
        if archive.exists():
            raise ValueError(f"Duplicate archive path: {archive}")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=5) as z:
            z.mkdir(root.name)
            for folder_name in sorted(
                {
                    str(parent)
                    for n in files
                    for parent in Path(n).parents
                    if parent != Path(".")
                }
            ):
                z.mkdir(root.name + "/" + Path(folder_name).as_posix())
            for key, value in sorted(files.items()):
                z.writestr(root.name + "/" + key, value)
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None
            for key, value in files.items():
                assert z.read(root.name + "/" + key) == value
            assert json.loads(z.read(root.name + "/res/data.json")) == data
        totals["templates"] += 1
        totals["outline entries"] += changed
        print(
            category,
            " | ",
            ", ".join(f"{a} -> {loaded[a][0]}" for a in sorted(aliases)),
            " | outlines",
            changed,
        )
    return totals


def main():
    parser = argparse.ArgumentParser(
        description="Build modified Storygramo template ZIPs from one local category."
    )
    parser.add_argument(
        "source", type=Path, help="Local category directory with unpacked templates"
    )
    parser.add_argument(
        "output", type=Path, help="New directory for the generated packages"
    )
    parser.add_argument("--audit", type=Path, default=BASE / "font_license_audit.csv")
    parser.add_argument("--matches", type=Path, default=BASE / "font-matches.csv")
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    if not source.is_dir():
        parser.error(f"Source directory does not exist: {source}")
    if output.exists() or output.is_relative_to(source):
        parser.error("Output must be a new directory outside the source category")
    audit = {row["File"].casefold(): row["Status"] for row in read_rows(args.audit)}
    choices = {row["Original alias"].casefold(): row for row in read_rows(args.matches)}
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=output.parent) as directory:
            staged = Path(directory) / "packages"
            staged.mkdir()
            totals = build(source, staged, audit, choices)
            staged.rename(output)
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f"Build failed: {error}\n")
    print(dict(totals))


if __name__ == "__main__":
    main()
