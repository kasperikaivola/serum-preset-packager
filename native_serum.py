"""Version-pinned, isolated Windows Serum 2 native FXP import backend.

Private importer addresses are enabled only for the verified 2.0.16 binary.
Each conversion runs in a separate process; the plugin is never loaded into a DAW.
"""
import ctypes as C, io, uuid, sys, os, json, struct, hashlib, subprocess, tempfile
from pathlib import Path
P=C.c_void_p; I=C.c_int32; Q=C.c_int64
F=getattr(C, 'WINFUNCTYPE', C.CFUNCTYPE)
def uid(s): return uuid.UUID(s).bytes_le
def call(obj,n,types=(),args=(),ret=I):
    vt=C.cast(obj,C.POINTER(C.POINTER(P))).contents
    return F(ret,P,*types)(vt[n])(obj,*args)
class Object:
    def __init__(self,methods,ids):
        self.ids=ids
        def query(this,iid,out):
            if C.string_at(iid,16) in self.ids:
                C.cast(out,C.POINTER(P))[0]=self.ptr; return 0
            C.cast(out,C.POINTER(P))[0]=None; return -2147467262
        self.callbacks=[F(I,P,P,P)(query),F(I,P)(lambda _:1),F(I,P)(lambda _:1)]
        self.callbacks += [F(I,P,*t)(fn) for t,fn in methods]
        self.vt=(P*len(self.callbacks))(*[C.cast(f,P) for f in self.callbacks])
        self.obj=(P*1)(C.cast(self.vt,P)); self.ptr=C.cast(self.obj,P)
class Stream(Object):
    def __init__(self,data=b''):
        self.io=io.BytesIO(data)
        def read(_,buf,n,out):
            b=self.io.read(n); C.memmove(buf,b,len(b))
            if out: C.cast(out,C.POINTER(I))[0]=len(b)
            return 0
        def write(_,buf,n,out):
            self.io.write(C.string_at(buf,n))
            if out: C.cast(out,C.POINTER(I))[0]=n
            return 0
        def seek(_,pos,mode,out):
            p=self.io.seek(pos,mode)
            if out: C.cast(out,C.POINTER(Q))[0]=p
            return 0
        def tell(_,out): C.cast(out,C.POINTER(Q))[0]=self.io.tell(); return 0
        super().__init__([((P,I,P),read),((P,I,P),write),((Q,I,P),seek),((P,),tell)], [uid('C3BF6EA2-3099-4752-9B6B-F9901EE33E9B')])
class Info(C.Structure):
    _fields_=[('cid',C.c_char*16),('cardinality',I),('category',C.c_char*32),('name',C.c_char*64)]
def name(_,buf):
    b='Preset converter\0'.encode('utf-16-le'); C.memmove(buf,b,len(b)); return 0
host=Object([((P,),name),((P,P,P),lambda *args:-2147467262)],[uid('58E595CC-DB2D-4969-8B6A-AF8C36A664E5')])

def string(p):
 size=C.c_uint64.from_address(p+16).value;cap=C.c_uint64.from_address(p+24).value
 address=C.c_uint64.from_address(p).value if cap>15 else p
 return C.string_at(address,size).decode('utf-8',errors='replace')
def decode(p,depth=0):
 if depth>40:raise ValueError('depth')
 kind=C.c_ubyte.from_address(p).value;value=C.c_uint64.from_address(p+8).value
 if kind==0:return None
 if kind==1:
  head=C.c_uint64.from_address(value).value;count=C.c_uint64.from_address(value+8).value;out={}
  def node(q):
   if q==head:return
   node(C.c_uint64.from_address(q).value)
   out[string(q+32)]=decode(q+64,depth+1)
   node(C.c_uint64.from_address(q+16).value)
  node(C.c_uint64.from_address(head+8).value)
  assert len(out)==count,(len(out),count)
  return out
 if kind==2:
  start=C.c_uint64.from_address(value).value;end=C.c_uint64.from_address(value+8).value
  return [decode(q,depth+1)for q in range(start,end,16)]
 if kind==3:return string(value)
 if kind==4:return bool(value & 0xff)  # C++ bool owns one byte; the rest is padding.
 if kind==5:return C.c_int64.from_address(p+8).value
 if kind==6:return value
 if kind==7:return C.c_double.from_address(p+8).value
 raise ValueError(('unknown json kind',kind,hex(p)))


BINARY_SHA256 = 'a2fd61e3269edcc693e4a845a72b80247ec939db43bc73f9a192a9f9566e617f'
DEFAULT_PLUGIN = Path(os.environ.get('CommonProgramFiles', 'C:/Program Files/Common Files')) / 'VST3/Serum2.vst3/Contents/x86_64-win/Serum2.vst3'


def _decode_state(raw):
    import cbor2, zstandard
    if raw[:9] != b'XferJson\0':
        raise ValueError('plugin returned an invalid state header')
    n, = struct.unpack_from('<Q', raw, 9)
    offset = 17 + n
    return json.loads(raw[17:offset]), cbor2.loads(zstandard.ZstdDecompressor().decompress(raw[offset+8:]))


def _worker(source, output, plugin, intermediate=False, canonicalize=False):
    from fxp_to_serumpreset import (encode_preset, _params, _wavetable,
                                  default_roots, _table_position, _scalars)
    import math
    from serum1_json import unpack_fxp
    if os.name != 'nt' or C.sizeof(P) != 8:
        raise ValueError('native conversion requires 64-bit Python on Windows')
    # Validate before loading executable code or using a private address.
    plugin = Path(plugin).resolve(strict=True)
    if hashlib.sha256(plugin.read_bytes()).hexdigest() != BINARY_SHA256:
        raise ValueError('native backend supports only the verified Serum 2.0.16 binary; use offline conversion for other versions')
    legacy = None if canonicalize else unpack_fxp(source)  # Validate before invoking the private importer.
    dll = C.WinDLL(str(plugin))
    if not dll.InitDll():
        raise ValueError('Serum InitDll failed')
    dll.GetPluginFactory.restype = P
    factory = dll.GetPluginFactory()
    comp = P()
    for i in range(call(factory, 4)):
        info = Info()
        if call(factory, 5, (I,P), (i,C.byref(info))) != 0:
            continue
        if info.category == b'Audio Module Class':
            cid = C.string_at(C.addressof(info),16)
            if call(factory, 6, (P,P,P), (cid,uid('E831FF31-F2D5-4301-928E-BBEE25697802'),C.byref(comp))) != 0:
                raise ValueError('cannot create Serum processor')
            break
    if not comp or call(comp,3,(P,),(host.ptr,)) != 0:
        raise ValueError('cannot initialize Serum processor')

    if canonicalize:
        obj=json.loads(Path(source).read_text(encoding='utf-8'))
        stream=Stream()
        if call(comp,13,(P,),(stream.ptr,)) != 0:
            raise ValueError('native initial state save failed')
        metadata,defaults=_decode_state(stream.io.getvalue())
        stream=Stream(encode_preset({'metadata':metadata,'data':{**defaults,**obj['data']}}))
        status=call(comp,12,(P,),(stream.ptr,))
        if status != 0:
            raise ValueError(f'standalone state failed native reload ({status})')
        stream=Stream()
        if call(comp,13,(P,),(stream.ptr,)) != 0:
            raise ValueError('native canonical state save failed')
        _,data=_decode_state(stream.io.getvalue())
        Path(output).write_text(json.dumps({'metadata':obj['metadata'],'data':data},ensure_ascii=False),encoding='utf-8')
        return

    @F(Q,P,P,P)
    def message(this,title,body):
        print('Serum:',C.string_at(title).decode(errors='replace'),C.string_at(body).decode(errors='replace'),flush=True)
        return 0
    vt=(P*3)(0,0,C.cast(message,P));sink=(P*1)(C.cast(vt,P))
    class Context(C.Structure):
        _fields_=[('flag',C.c_uint64),('sink',P)]
    context=Context(0,C.cast(sink,P));native_doc=(C.c_uint64*2)(0,0)
    raw=Path(source).read_bytes();payload=C.create_string_buffer(raw[60:])
    importer=F(Q,P,P,P,I,I,P)(dll._handle+0x2a57d0)
    if importer(C.byref(context),C.byref(native_doc),payload,len(raw)-60,raw[19]==ord('Y'),C.create_string_buffer(str(Path(source)).encode('utf-8'))) != 1:
        raise ValueError('native FXP importer rejected the preset')
    imported=decode(C.addressof(native_doc))
    metadata={k:imported[k] for k in ('fileType','presetAuthor','presetDescription','presetName','product','productVersion','url','vendor','version') if k in imported}
    metadata['tags']=[]
    if intermediate:
        result={'metadata':metadata,'data':imported,'nativeImportStage':'intermediate'}
    else:
        def state():
            stream=Stream()
            if call(comp,13,(P,),(stream.ptr,)) != 0:
                raise ValueError('native state save failed')
            return stream.io.getvalue()
        processor_metadata,defaults=_decode_state(state())
        data={**defaults,**imported}
        # A newly created processor locks these controls. GUI preset import
        # loads the preset's settings instead; otherwise tuning can be lost.
        data['lockOversampling']=False
        data['lockTuning']=False
        # The legacy importer writes an integer here; the engine's JSON reader
        # requires a boolean. This is a type conversion, not a guessed setting.
        if 'mpeEnabled' in data:
            if data['mpeEnabled'] not in (0,1,False,True):
                raise ValueError('invalid legacy MPE flag')
            data['mpeEnabled']=bool(defaults['mpeEnabled'])
        stream=Stream(encode_preset({'metadata':dict(processor_metadata),'data':data}))
        status=call(comp,12,(P,),(stream.ptr,))
        if status not in (0,1):
            raise ValueError(f'native intermediate load failed ({status})')
        # Sparse legacy module JSON can return incomplete (1). Save the engine's
        # completed state, then require a clean reload before publishing it.
        _,completed=_decode_state(state())
        # Engine-only loading does not run the GUI wavetable position update.
        # Share the rules already established against native pair fixtures.
        blob=struct.pack(f"<{len(legacy['data']['stateWords'])}I", *legacy['data']['stateWords'])
        embedded=struct.pack(f"<{len(legacy['data']['wavetableWords'])}I", *legacy['data']['wavetableWords']) + bytes(legacy['data']['wavetableTailBytes'])
        parameters=_params(blob)
        middle_modern=len(blob)==28232 and any(
            math.isclose(n*95,round(n*95),abs_tol=2e-5)
            and not math.isclose(n*89,round(n*89),abs_tol=2e-5)
            for n in (parameters[44],parameters[142]))
        for osc,offset in enumerate((1,14)):
            try:
                wt=_wavetable(blob,embedded,osc,default_roots())
            except ValueError:
                # The engine can resolve assets that the portable reader cannot.
                wt={}
            interpolation=wt.pop('_legacyInterpolation',None)
            # Use the engine's resolved descriptor when a library path is not
            # available to the portable asset reader.
            saved=completed[f'Oscillator{osc}'][f'WTOsc{osc}']
            if 'numFrames' not in wt and 'embeddedWTData' not in wt and 'numFrames' in saved:
                wt['numFrames']=saved['numFrames']
            warp=round(parameters[168+osc]*23)
            position=_table_position(blob,parameters,offset,wt,warp,middle_modern,osc,interpolation)
            plain=saved['plainParams']
            if plain=='default':
                plain={};saved['plainParams']=plain
            plain['kParamTablePos']=1+255*position
        # Scalar response curves are already migrated by the native importer.
        # Avoid calling standalone validation for features this backend supports.
        voice=completed.get('VoiceFilter0',{}).get('plainParams',{})
        if isinstance(voice,dict) and 'kParamFreq' in voice:
            voice['kParamFreq']=min(voice['kParamFreq'],.9997483491897583)
        for key in ('presetName','presetAuthor','presetDescription','fileType','WTOsc','tuningName','tuningData'):
            if key in imported:
                completed[key]=imported[key]
        result={'metadata':metadata,'data':completed}
        stream=Stream(encode_preset({'metadata':dict(processor_metadata),'data':completed}))
        if call(comp,12,(P,),(stream.ptr,)) != 0:
            raise ValueError('completed native preset failed reload validation')
    Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


def native_import(source, *, plugin=None, intermediate=False, timeout=60, _canonicalize=False):
    """Return migrated JSON or a completed preset wrapper from an isolated worker."""
    if os.name != 'nt':
        raise ValueError('native conversion requires Windows and the verified Serum 2.0.16 installation')
    with tempfile.TemporaryDirectory(prefix='serum-native-') as directory:
        output=Path(directory)/'result.json'
        command=[sys.executable,str(Path(__file__).resolve()),'--worker',str(Path(source).resolve()),str(output),str(plugin or DEFAULT_PLUGIN)]
        if intermediate:
            command.append('--intermediate')
        if _canonicalize:
            command.append('--canonicalize')
        try:
            process=subprocess.run(command,capture_output=True,timeout=timeout,text=True,encoding='utf-8',errors='replace')
        except subprocess.TimeoutExpired as error:
            raise ValueError(f'native conversion exceeded {timeout} seconds') from error
        if process.returncode or not output.is_file():
            raise ValueError('native conversion failed: '+(process.stderr or process.stdout)[-2000:])
        return json.loads(output.read_text(encoding='utf-8'))


def canonicalize(obj, *, plugin=None, timeout=60):
    """Read a generated state back through Serum for development comparisons."""
    with tempfile.TemporaryDirectory(prefix='serum-canonical-') as directory:
        source=Path(directory)/'input.json'
        source.write_text(json.dumps(obj,ensure_ascii=False),encoding='utf-8')
        return native_import(source,plugin=plugin,timeout=timeout,_canonicalize=True)


if __name__ == '__main__':
    if len(sys.argv) >= 5 and sys.argv[1] == '--worker':
        try:
            _worker(sys.argv[2],sys.argv[3],sys.argv[4],'--intermediate' in sys.argv[5:],'--canonicalize' in sys.argv[5:])
        except Exception as error:
            print(f'{type(error).__name__}: {error}',file=sys.stderr,flush=True)
            os._exit(1)
        # DLL process teardown can invoke callbacks after Python destroys them.
        # These resources belong to this disposable worker process.
        sys.stdout.flush();os._exit(0)
    raise SystemExit('Use fxp_to_serumpreset.py --backend native, or cli.py import-fxp.')
