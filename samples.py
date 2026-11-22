"""Non-deliverable examples for isolated demo mode and tests."""
import io
import zipfile
from email.message import EmailMessage
from email import policy


def workbook_bytes(group):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="志愿者名单" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>组别</t></is></c><c r="B1" t="inlineStr"><is><t>姓名（示例）</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>第%s组</t></is></c><c r="B2" t="inlineStr"><is><t>示例志愿者</t></is></c></row></sheetData></worksheet>' % group)
    return stream.getvalue()


def sample_messages():
    for group, name in ((10, '陈同学'), (11, '林同学'), (12, '周同学')):
        msg = EmailMessage(policy=policy.SMTP)
        msg['From'] = '示例组织者 <organizer@example.com>'
        msg['To'] = '%s <group%s@example.com>' % (name, group)
        if group == 10:
            msg['Cc'] = 'coordinator@example.com'
            msg['Bcc'] = 'archive@example.com'
        msg['Subject'] = '第 %s 组志愿者名单与活动安排' % group
        msg['X-Unsent'] = '1'
        msg.set_content('%s，你好！\n\n附件是第 %s 组的志愿者名单，请查收并核对。\n\n活动时间：周六 09:00\n集合地点：校园志愿服务中心\n\n这是一封本机演示邮件，所有姓名和地址均为示例。\n\n谢谢！\n志愿服务工作组' % (name, group))
        msg.add_alternative('<h2 style="color:#5849d7">志愿服务 · 第 %s 组</h2><p>%s，你好！</p><p>附件是本组的<strong>志愿者名单</strong>，请查收并核对。</p><table cellpadding="8"><tr><td>活动时间</td><td>周六 09:00</td></tr><tr><td>集合地点</td><td>校园志愿服务中心</td></tr></table><p>这是一封本机演示邮件，所有姓名和地址均为示例。</p>' % (group, name), subtype='html')
        msg.add_attachment(workbook_bytes(group), maintype='application', subtype='vnd.openxmlformats-officedocument.spreadsheetml.sheet', filename='第%s组-志愿者名单.xlsx' % group)
        yield '%s组-%s.eml' % (group, name), msg.as_bytes()
    bad = EmailMessage(policy=policy.SMTP)
    bad['Subject'] = '待补充收件人的草稿（示例）'
    bad.set_content('此示例用于展示导入检查，不允许发送。')
    yield '检查示例-缺少收件人.eml', bad.as_bytes()
