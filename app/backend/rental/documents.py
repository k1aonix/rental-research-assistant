"""Bounded document decoding. No external URLs, macros, or executable previews."""
import base64
import io
import zipfile
from pathlib import Path
from .common import DomainError

MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 20

def decode_upload(request):
    if not request.additional_images:return decode_document(request.file_name,request.content_base64)
    files=[request,*request.additional_images];pages=[];buffer=io.BytesIO();total=0
    with zipfile.ZipFile(buffer,'w') as archive:
        for i,file in enumerate(files,1):
            if Path(file.file_name).suffix.lower() not in ('.png','.jpg','.jpeg'):
                raise DomainError('INVALID_FILE','多文件上传仅支持按选择顺序排列的JPG/PNG。',422)
            raw,decoded=decode_document(file.file_name,file.content_base64);total+=len(raw)
            if total>MAX_BYTES:raise DomainError('FILE_SIZE_LIMIT','整组图片最多10MB。',413)
            pages.append({**decoded[0],'page':i})
            archive.writestr(str(i)+Path(file.file_name).suffix.lower(),raw)
    return buffer.getvalue(),pages

def decode_document(name, encoded):
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise DomainError('INVALID_FILE', '文件编码无效。', 422) from None
    if not raw or len(raw) > MAX_BYTES:
        raise DomainError('FILE_SIZE_LIMIT', '每份合同须为1字节至10MB。', 413)
    suffix = Path(name).suffix.lower()
    try:
        pages=[]
        if suffix == '.pdf':
            from pypdf import PdfReader
            reader=PdfReader(io.BytesIO(raw))
            if reader.is_encrypted:
                raise DomainError('ENCRYPTED_FILE', '请先解除PDF密码。', 422)
            if not 1 <= len(reader.pages) <= MAX_PAGES:
                raise DomainError('PAGE_LIMIT', '合同最多20页，未截断分析。', 422)
            for i,page in enumerate(reader.pages):
                text=page.extract_text() or ''
                # Image-containing PDFs also need a full page OCR to avoid silently missing clauses.
                if len(text.strip()) < 30 or len(page.images):
                    import pypdfium2 as pdfium
                    pdf=pdfium.PdfDocument(raw)
                    try:
                        width,height=pdf[i].get_size()
                        if width*height*2.25>16000000:
                            raise DomainError('PAGE_SIZE_LIMIT','PDF页面尺寸过大，请提供正常纸张尺寸文件。',422)
                        image=pdf[i].render(scale=1.5).to_pil()
                        buf=io.BytesIO();image.save(buf,format='PNG')
                        pages.append({'page':i+1,'image':'data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode()})
                    finally: pdf.close()
                else: pages.append({'page':i+1,'text':text})
        elif suffix == '.docx':
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if sum(x.file_size for x in archive.infolist()) > 30*1024*1024 or len(archive.infolist()) > 2000:
                    raise DomainError('FILE_SIZE_LIMIT','DOCX解压大小超限。',422)
            from docx import Document
            doc=Document(io.BytesIO(raw))
            # Preserve body paragraph/table order; headers/footers are included explicitly.
            from docx.text.paragraph import Paragraph
            from docx.table import Table
            parts=[]
            for node in doc.element.body:
                if node.tag.endswith('}p'): parts.append(Paragraph(node,doc).text)
                elif node.tag.endswith('}tbl'): parts.extend(' | '.join(c.text for c in row.cells) for row in Table(node,doc).rows)
            for section in doc.sections:
                parts.extend(p.text for part in [section.header,section.footer] for p in part.paragraphs)
            pages=[{'page':1,'text':'\n'.join(parts),'location_type':'paragraphs'}]
            # Preserve embedded images as additional identified locations; never ignore scanned clauses.
            from docx.oxml.ns import qn
            for part in [doc.part]+[section.header.part for section in doc.sections]+[section.footer.part for section in doc.sections]:
                for node in part.element.iter(qn('a:blip')):
                    image_part=part.related_parts.get(node.get(qn('r:embed')))
                    if image_part is None:raise DomainError('DOCUMENT_READ_FAILED','DOCX外链图片无法读取，请导出PDF。',422)
                    _,image_pages=decode_document('image.'+image_part.content_type.split('/')[-1].replace('jpeg','jpg'),base64.b64encode(image_part.blob).decode())
                    pages.append({'page':len(pages)+1,'image':image_pages[0]['image'],'location_type':'embedded_image'})
                    if len(pages)>MAX_PAGES:raise DomainError('PAGE_LIMIT','DOCX图片数量超限，请拆分或导出PDF。',422)
        elif suffix in ('.jpg','.jpeg','.png'):
            from PIL import Image
            Image.MAX_IMAGE_PIXELS=16000000
            image=Image.open(io.BytesIO(raw))
            if image.format not in ('JPEG','PNG') or getattr(image,'n_frames',1)!=1 or image.width*image.height>16000000:
                raise DomainError('INVALID_FILE','仅支持单页JPG/PNG，最多1600万像素。',422)
            image.load();buf=io.BytesIO();image.convert('RGB').save(buf,format='JPEG',quality=90)
            pages=[{'page':1,'image':'data:image/jpeg;base64,'+base64.b64encode(buf.getvalue()).decode()}]
        else: raise DomainError('FILE_TYPE_UNSUPPORTED','仅支持PDF、DOCX、JPG和PNG。',422)
        if not pages or all(not p.get('image') and not p.get('text','').strip() for p in pages):
            raise DomainError('DOCUMENT_EMPTY','文档无可读取文字。',422)
        if sum(len(p.get('text','')) for p in pages)>80000:
            raise DomainError('DOCUMENT_LENGTH_LIMIT','合同超过80000字符，未截断。',422)
        return raw,pages
    except DomainError: raise
    except Exception:
        raise DomainError('DOCUMENT_READ_FAILED','文件读取失败、损坏或处理组件不可用；未进行检查。',422) from None
