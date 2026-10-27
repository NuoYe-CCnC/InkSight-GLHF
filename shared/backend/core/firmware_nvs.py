"""Read-only NVS protection fingerprint, independent of third-party packages.

Format: ESP-IDF NVS internals (4096-byte pages / 32-byte entries). Invalid,
transient, encrypted or unsupported records fail closed; never repairs NVS.
"""
import binascii
import hashlib
import json
import struct

RUNTIME_KEYS={'p3_ai_key','p3_ai_chg','p3_ai_hold','panel_page','retry_count',
              'net_fail_n','net_retry_at','v4_cu_req','news_req_id','news_req_at','live_boot_mk'}


def crc(raw):return binascii.crc32(raw,0xffffffff)&0xffffffff


def decode(raw):
    if not raw or len(raw)%4096:raise ValueError('NVS分区长度无效')
    pages=[];entries=[]
    for start in range(0,len(raw),4096):
        page=raw[start:start+4096]
        if page==b'\xff'*4096:continue
        state,sequence=struct.unpack_from('<II',page)
        if state not in {0xfffffffe,0xfffffffc} or page[8] not in {0xff,0xfe}:
            raise ValueError('NVS状态尚未稳定或格式不受支持')
        if crc(page[4:28])!=struct.unpack_from('<I',page,28)[0]:raise ValueError('NVS页校验失败')
        pages.append((sequence,page))
    for sequence,page in sorted(pages):
        bitmap=page[32:64]
        state=lambda index:(bitmap[index//4]>>((index%4)*2))&3
        index=0
        while index<126:
            if state(index)!=2:index+=1;continue
            item=page[64+index*32:96+index*32]
            ns,kind,span,chunk=item[:4]
            if not 1<=span<=126-index or any(state(i)!=2 for i in range(index,index+span)):
                raise ValueError('NVS记录片段不完整')
            if crc(item[:4]+item[8:])!=struct.unpack_from('<I',item,4)[0]:raise ValueError('NVS记录校验失败')
            key=item[8:24].split(b'\0',1)[0].decode('ascii')
            numbers={1:(1,False),2:(2,False),4:(4,False),8:(8,False),
                     0x11:(1,True),0x12:(2,True),0x14:(4,True),0x18:(8,True)}
            if kind in numbers:
                if span!=1:raise ValueError('NVS整数片段异常')
                size,signed=numbers[kind];value=int.from_bytes(item[24:24+size],'little',signed=signed)
            elif kind in {0x21,0x41,0x42}:
                size=struct.unpack_from('<H',item,24)[0]
                data=page[96+index*32:64+(index+span)*32][:size]
                if size>32*(span-1) or crc(data)!=struct.unpack_from('<I',item,28)[0]:raise ValueError('NVS值校验失败')
                if kind==0x21:
                    if not data.endswith(b'\0'):raise ValueError('NVS字符串未结束')
                    value=data[:-1].decode('utf8')
                else:value={'blob_sha256':hashlib.sha256(data).hexdigest(),'chunk':chunk}
            elif kind==0x48:value={'blob_index':item[24:].hex()}
            else:raise ValueError('NVS包含未知类型')
            entries.append((ns,key,value));index+=span
    if any(not isinstance(value,int) or not 1<=value<=254 for ns,key,value in entries if ns==0):raise ValueError('NVS命名空间标识无效')
    namespaces={value:key for ns,key,value in entries if ns==0}
    output={}
    for ns,key,value in entries:
        if ns==0:continue
        if ns not in namespaces:raise ValueError('NVS命名空间缺失')
        output.setdefault(namespaces[ns],{})[key]=value
    return output


def protected(raw):
    namespace=decode(raw).get('inksight',{})
    if namespace.get('cfg_version')!=2:raise ValueError('现有设备配置版本不受支持，拒绝自动迁移覆盖')
    count=namespace.get('wifi_n')
    if type(count) is not int or not 0<=count<=5:raise ValueError('现有Wi-Fi列表格式不受支持，拒绝隐式迁移')
    # The supported application's settings are strings/integers. Do not
    # mistake a blob's size/index for its content or accept unknown schemas.
    if any(isinstance(value,dict) for value in namespace.values()):
        raise ValueError('现有项目配置包含未支持的二进制字段，拒绝自动更新')
    values={key:value for key,value in namespace.items() if key not in RUNTIME_KEYS}
    return hashlib.sha256(json.dumps(values,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
