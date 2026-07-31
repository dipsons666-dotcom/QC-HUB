import zipfile, xml.etree.ElementTree as ET
from pathlib import Path
p = Path(r'c:\Users\Taofeek Olatunji\Documents\QC-HUB\QC FLAGS DEFINITIONS.docx')
print('exists', p.exists(), 'size', p.stat().st_size if p.exists() else None)
with zipfile.ZipFile(p) as z:
    root = ET.fromstring(z.read('word/document.xml'))
    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    parts = []
    for t in root.findall('.//w:t', ns):
        if t.text:
            parts.append(t.text)
    text = ''.join(parts)
    print(text[:30000])
