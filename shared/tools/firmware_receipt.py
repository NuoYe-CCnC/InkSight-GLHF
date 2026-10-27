#!/usr/bin/env python3
"""Bounded USB network receipt, no reset lines, no private raw-log output."""
import argparse
import json
import time
import serial


def verified_receipt(line,build_id):
    prefix=b'[INKSIGHT_RECEIPT] '
    if not line.startswith(prefix) or len(line)>2048:return None
    try:data=json.loads(line[len(prefix):])
    except (ValueError,UnicodeError):return None
    if (not isinstance(data,dict) or type(data.get('schema')) is not int or data.get('schema')!=1 or data.get('build_id')!=build_id
        or data.get('compatibility')!='INKSIGHT_COMPAT_V1_SSD1677_N32R16'
        or data.get('network_json') is not True or data.get('physical_confirmed') is not False):return None
    return {'network_verified':True,'transport':'physical-USB-after-verified-image',
            'build_id':build_id,'physical_confirmed':False}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',required=True)
    parser.add_argument('--build-id',required=True);parser.add_argument('--seconds',type=int,default=70)
    args=parser.parse_args()
    port=serial.Serial(port=None,baudrate=115200,timeout=.2)
    port.port=args.port;port.dtr=False;port.rts=False;port.open()
    deadline=time.monotonic()+min(90,max(1,args.seconds));buffer=b''
    try:
        while time.monotonic()<deadline:
            buffer+=port.read(min(4096,port.in_waiting or 1))
            if len(buffer)>8192:buffer=buffer[-4096:]
            while b'\n' in buffer:
                line,buffer=buffer.split(b'\n',1)
                result=verified_receipt(line.rstrip(b'\r'),args.build_id)
                if result:print(json.dumps(result));return 0
    finally:port.close()
    print(json.dumps({'network_verified':False,'physical_confirmed':False,'reason':'receipt_not_seen'}))
    return 0


if __name__=='__main__':raise SystemExit(main())
