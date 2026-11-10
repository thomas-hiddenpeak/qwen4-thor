"""Original deterministic Office fixtures, built with Python's standard library."""
from io import BytesIO
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P = 'http://schemas.openxmlformats.org/package/2006/relationships'
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
D = 'http://schemas.openxmlformats.org/drawingml/2006/main'
T = 'http://schemas.openxmlformats.org/presentationml/2006/main'
MC = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
O = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
OT = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'
TABLE = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'
DRAW = 'urn:oasis:names:tc:opendocument:xmlns:drawing:1.0'


def rels(items):
    return f'<Relationships xmlns="{P}">' + ''.join(
        f'<Relationship Id="{id}" Type="{R}/{kind}" Target="{target}"/>'
        for id, kind, target in items) + '</Relationships>'


def package(entries):
    result = BytesIO()
    with zipfile.ZipFile(result, 'w') as archive:
        for name, value in entries.items():
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, value.encode() if isinstance(value, str) else value)
    return result.getvalue()


def save(name, entries):
    (ROOT / name).write_bytes(package(entries))


def base(main):
    return {'[Content_Types].xml':
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
            '_rels/.rels': rels([('root', 'officeDocument', main)])}


def word():
    entries = base('word/document.xml')
    entries['word/document.xml'] = f'''<w:document xmlns:w="{W}" xmlns:r="{R}"
        xmlns:mc="{MC}" xmlns:future="urn:unimplemented"><w:body>
      <w:p><w:r><w:t>Report 中文 😀</w:t><w:tab/><w:t>tail</w:t></w:r>
        <w:del><w:r><w:t>DELETED SECRET</w:t></w:r></w:del></w:p>
      <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Left</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Right</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
      <mc:AlternateContent><mc:Choice Requires="future"><w:p><w:r><w:t>BAD CHOICE</w:t></w:r></w:p></mc:Choice>
        <mc:Fallback><w:p><w:r><w:t>Fallback once</w:t></w:r></w:p></mc:Fallback></mc:AlternateContent>
      <mc:AlternateContent><mc:Choice Requires="w"><w:p><w:r><w:t>Choice once</w:t></w:r></w:p></mc:Choice>
        <mc:Fallback><w:p><w:r><w:t>BAD FALLBACK</w:t></w:r></w:p></mc:Fallback></mc:AlternateContent>
      <w:p><w:r><w:footnoteReference w:id="2"/><w:commentReference w:id="7"/></w:r></w:p>
      <w:altChunk r:id="alt"/><w:sectPr><w:headerReference r:id="header"/></w:sectPr>
      </w:body></w:document>'''
    entries['word/_rels/document.xml.rels'] = rels([
        ('header', 'header', 'header.xml'), ('foot', 'footnotes', 'footnotes.xml'),
        ('comment', 'comments', 'comments.xml'), ('alt', 'aFChunk', 'import.rtf')])
    entries['word/header.xml'] = f'<w:hdr xmlns:w="{W}"><w:p><w:r><w:t>Real header</w:t></w:r></w:p></w:hdr>'
    entries['word/footnotes.xml'] = f'''<w:footnotes xmlns:w="{W}">
       <w:footnote w:id="1"><w:p><w:r><w:t>UNUSED FOOTNOTE</w:t></w:r></w:p></w:footnote>
       <w:footnote w:id="2"><w:p><w:r><w:t>Selected footnote</w:t></w:r></w:p></w:footnote></w:footnotes>'''
    entries['word/comments.xml'] = f'<w:comments xmlns:w="{W}"><w:comment w:id="7"><w:p><w:r><w:t>Actual comment</w:t></w:r></w:p></w:comment></w:comments>'
    entries['word/import.rtf'] = r'{\rtf1\ansi Imported \u20013?\u25991?\par}'
    save('word.docx', entries)
    strict = {key: value.replace(W, 'http://purl.oclc.org/ooxml/wordprocessingml/main')
              .replace(R, 'http://purl.oclc.org/ooxml/officeDocument/relationships')
              for key, value in entries.items()}
    save('strict.docx', strict)
    broken = dict(entries)
    broken['word/document.xml'] += '<broken>'
    save('broken.docx', broken)
    missing = dict(entries)
    missing['word/_rels/document.xml.rels'] = entries['word/_rels/document.xml.rels'].replace('header.xml', '../../outside.xml')
    save('bad-target.docx', missing)
    external = dict(entries)
    external['word/_rels/document.xml.rels'] = entries['word/_rels/document.xml.rels'].replace('Target="header.xml"', 'Target="file:///etc/hostname" TargetMode="External"')
    save('external.docx', external)
    nested = base('word/document.xml')
    nested['word/document.xml'] = f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body><w:altChunk r:id="nested"/></w:body></w:document>'
    nested['word/_rels/document.xml.rels'] = rels([('nested', 'aFChunk', 'nested.docx')])
    nested['word/nested.docx'] = package(entries)
    save('nested.docx', nested)


def slides():
    entries = base('ppt/presentation.xml')
    entries['ppt/presentation.xml'] = f'<p:presentation xmlns:p="{T}" xmlns:r="{R}"><p:sldIdLst><p:sldId id="300" r:id="two"/><p:sldId id="301" r:id="one"/></p:sldIdLst></p:presentation>'
    entries['ppt/_rels/presentation.xml.rels'] = rels([
        ('one', 'slide', 'slides/slide1.xml'), ('two', 'slide', 'slides/slide10.xml')])
    for name, text in [('slide10.xml', 'First actual slide'), ('slide1.xml', 'Second actual slide')]:
        entries['ppt/slides/' + name] = f'<p:sld xmlns:p="{T}" xmlns:a="{D}"><p:cSld><p:spTree><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:spTree></p:cSld></p:sld>'
    entries['ppt/slides/_rels/slide10.xml.rels'] = rels([('notes', 'notesSlide', '../notesSlides/notes1.xml')])
    entries['ppt/notesSlides/notes1.xml'] = f'<p:notes xmlns:p="{T}" xmlns:a="{D}"><a:p><a:r><a:t>Speaker notes</a:t></a:r></a:p></p:notes>'
    save('slides.pptx', entries)


def sheets():
    entries = base('xl/workbook.xml')
    entries['xl/workbook.xml'] = f'<workbook xmlns="{S}" xmlns:r="{R}"><workbookPr date1904="1"/><sheets><sheet name="真实数据" sheetId="1" r:id="data"/><sheet name="Tail" sheetId="2" r:id="tail"/><sheet name="Chart" sheetId="3" r:id="chart"/></sheets></workbook>'
    entries['xl/_rels/workbook.xml.rels'] = rels([
        ('data', 'worksheet', 'worksheets/data.xml'), ('tail', 'worksheet', 'worksheets/tail.xml'),
        ('chart', 'chartsheet', 'chartsheets/chart.xml'), ('shared', 'sharedStrings', 'sharedStrings.xml'),
        ('styles', 'styles', 'styles.xml')])
    entries['xl/sharedStrings.xml'] = f'<sst xmlns="{S}"><si><r><t>_x4E2D__x6587_ </t></r><r><t>_xD83D__xDE00_ literal_x005F_x0041_</t></r><rPh><t>DO NOT APPEND</t></rPh></si></sst>'
    entries['xl/styles.xml'] = f'<styleSheet xmlns="{S}"><numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00"/></numFmts><cellXfs count="3"><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/></cellXfs><dxfs><dxf><numFmt numFmtId="164" formatCode="0.000"/></dxf></dxfs></styleSheet>'
    entries['xl/worksheets/data.xml'] = f'''<worksheet xmlns="{S}"><sheetData>
      <row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="inlineStr"><is><t>inline_x000A_line</t></is></c></row>
      <row r="3"><c r="A3" s="2"><f>SUM(20,22)</f><v> +42.0 </v></c>
       <c r="B3" s="1"><v>45000</v></c><c r="C3" t="b"><v>1</v></c>
       <c r="D3"><f t="shared" si="7"/></c><c r="E3" t="e"><v>#DIV/0!</v></c></row>
      </sheetData></worksheet>'''
    entries['xl/worksheets/tail.xml'] = f'<worksheet xmlns="{S}"><sheetData>' + ''.join(
        f'<row r="{i}"><c r="A{i}" t="str"><v>row {i}</v></c></row>' for i in range(1, 1003)) + '</sheetData></worksheet>'
    entries['xl/chartsheets/chart.xml'] = f'<chartsheet xmlns="{S}"/>'
    save('sheets.xlsx', entries)
    for name, before, after in [('bad-shared.xlsx', '<v>0</v>', '<v>999</v>'),
                                 ('bad-numeric.xlsx', ' +42.0 ', 'NaN'),
                                 ('bad-position.xlsx', 'r="C1"', 'r="A1"')]:
        bad = dict(entries)
        bad['xl/worksheets/data.xml'] = bad['xl/worksheets/data.xml'].replace(before, after)
        save(name, bad)
    no_style = base('xl/workbook.xml')
    no_style['xl/workbook.xml'] = f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="Implicit" sheetId="1" r:id="data"/></sheets></workbook>'
    no_style['xl/_rels/workbook.xml.rels'] = rels([('data', 'worksheet', 'sheet.xml')])
    no_style['xl/sheet.xml'] = f'<worksheet xmlns="{S}"><sheetData><row><c s="0"><v>7</v></c></row></sheetData></worksheet>'
    save('implicit.xlsx', no_style)


def odf():
    def document(kind, body):
        return f'<office:document-content xmlns:office="{O}" xmlns:text="{OT}" xmlns:table="{TABLE}" xmlns:draw="{DRAW}"><office:body><office:{kind}>{body}</office:{kind}></office:body></office:document-content>'
    text = '<text:p>真实<text:s text:c="3"/>spaces<text:tab/>tab<text:line-break/>line</text:p><text:tracked-changes><text:p>DELETED ODF</text:p></text:tracked-changes>'
    save('text.odt', {'mimetype': 'application/vnd.oasis.opendocument.text', 'content.xml': document('text', text)})
    save('slides.odp', {'mimetype': 'application/vnd.oasis.opendocument.presentation', 'content.xml': document('presentation', '<draw:page draw:name="Opening">' + text + '</draw:page>')})
    body = '''<table:table table:name="Repeated">
      <table:table-row><table:table-cell table:number-columns-repeated="2" office:value-type="string"><text:p>alpha</text:p></table:table-cell>
       <table:table-cell table:number-columns-repeated="2"/><table:table-cell office:value-type="float" office:value="5" table:formula="of:=SUM([.A1:.B1])"><text:p>5.00</text:p></table:table-cell>
       <table:table-cell table:number-columns-repeated="100000"/></table:table-row>
      <table:table-row table:number-rows-repeated="1005"><table:table-cell office:value-type="date" office:date-value="2026-11-10"/></table:table-row>
      </table:table><table:table table:name="Empty"><table:table-row><table:table-cell table:number-columns-repeated="100000"/></table:table-row></table:table>'''
    save('sheets.ods', {'mimetype': 'application/vnd.oasis.opendocument.spreadsheet', 'content.xml': document('spreadsheet', body)})


if __name__ == '__main__':
    word()
    slides()
    sheets()
    odf()
