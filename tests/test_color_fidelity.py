"""Policy-1 fidelity references; runs in an isolated media runtime with fresh /data."""
import ctypes as C
import json
import shutil
import struct
import subprocess as sp
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, '/app')
import main
import utils

PROFILES=Path('/usr/share/color/icc/ghostscript')
def run(args, data=None):return sp.check_output(args,input=data)
class FidelityTest(unittest.TestCase):
    def setUp(self):
        main.TMP_DIR.mkdir(parents=True,exist_ok=True)
        self.work=Path(tempfile.mkdtemp(dir=main.TMP_DIR))
    def tearDown(self):shutil.rmtree(self.work)
    def convert(self, src, fmt='png', **kw):
        out=self.work/('result.'+fmt)
        sp.run(utils.build_command([str(src)],str(out),fmt,0,0,None,None,100,still=True,**kw),check=True,stdout=sp.PIPE,stderr=sp.PIPE)
        return out
    def test_exif_orientations_1_to_8_are_baked_before_stripping_and_rotation(self):
        ppm=b'P6\n3 2\n255\n'+bytes([255,0,0,0,255,0,0,0,255,255,255,0,0,255,255,255,0,255])
        jpeg=run(['convert','ppm:-','-sampling-factor','4:4:4','-quality','100','jpeg:-'],ppm)
        original=run(['convert','jpeg:-','-depth','8','rgb:-'],jpeg)
        pixels=[original[i:i+3] for i in range(0,len(original),3)]
        orders=[[0,1,2,3,4,5],[2,1,0,5,4,3],[5,4,3,2,1,0],[3,4,5,0,1,2],[0,3,1,4,2,5],[3,0,4,1,5,2],[5,2,4,1,3,0],[2,5,1,4,0,3]]
        for orientation,order in enumerate(orders,1):
            exif=b'Exif\0\0II'+struct.pack('<HIH',42,8,1)+struct.pack('<HHI',0x112,3,1)+struct.pack('<H',orientation)+b'\0\0'+struct.pack('<I',0)
            src=self.work/'oriented.jpg';src.write_bytes(jpeg[:2]+b'\xff\xe1'+struct.pack('>H',len(exif)+2)+exif+jpeg[2:])
            out=self.convert(src,strip=True)
            actual=run(['convert',str(out),'-depth','8','rgb:-'])
            self.assertEqual(actual,b''.join(pixels[i] for i in order),orientation)
        out=self.convert(src,rotate=90,strip=True)
        self.assertEqual(run(['convert',str(out),'-depth','8','rgb:-']),original)
        oriented_jpeg=self.convert(src,'jpg')
        self.assertIn(run(['identify','-format','%[orientation]',str(oriented_jpeg)]).decode(),('TopLeft','Undefined'))
        self.assertEqual(main._read_output_dims(oriented_jpeg),(2,3))
    def test_tagged_rgb_adobergb_display_p3_and_cmyk_match_littlecms(self):
        lib=C.CDLL('liblcms2.so.2')
        def bind(name,args,result):
            f=getattr(lib,name);f.argtypes=args;f.restype=result;return f
        openp=bind('cmsOpenProfileFromFile',[C.c_char_p,C.c_char_p],C.c_void_p)
        close=bind('cmsCloseProfile',[C.c_void_p],C.c_int)
        create=bind('cmsCreateTransform',[C.c_void_p,C.c_uint,C.c_void_p,C.c_uint,C.c_uint,C.c_uint],C.c_void_p)
        do=bind('cmsDoTransform',[C.c_void_p,C.c_void_p,C.c_void_p,C.c_uint],None)
        delete=bind('cmsDeleteTransform',[C.c_void_p],None)
        class xyY(C.Structure):_fields_=[('x',C.c_double),('y',C.c_double),('Y',C.c_double)]
        class Primaries(C.Structure):_fields_=[('Red',xyY),('Green',xyY),('Blue',xyY)]
        curve=bind('cmsBuildParametricToneCurve',[C.c_void_p,C.c_int,C.POINTER(C.c_double)],C.c_void_p)(None,4,(C.c_double*7)(2.4,1/1.055,.055/1.055,1/12.92,.04045,0,0))
        p3=bind('cmsCreateRGBProfile',[C.POINTER(xyY),C.POINTER(Primaries),C.POINTER(C.c_void_p)],C.c_void_p)(C.byref(xyY(.3127,.329,1)),C.byref(Primaries(xyY(.68,.32,1),xyY(.265,.69,1),xyY(.15,.06,1))),(C.c_void_p*3)(curve,curve,curve))
        p3path=self.work/'display-p3.icc'
        self.assertTrue(bind('cmsSaveProfileToFile',[C.c_void_p,C.c_char_p],C.c_int)(p3,str(p3path).encode()))
        close(p3);bind('cmsFreeToneCurve',[C.c_void_p],None)(curve)
        for profile,mode,pixel in [(PROFILES/'srgb.icc','RGB',bytes([180,80,30])),(PROFILES/'a98.icc','RGB',bytes([180,80,30])),(p3path,'RGB',bytes([180,80,30])),(PROFILES/'default_cmyk.icc','CMYK',bytes([20,130,100,30]))]:
            src=self.work/'tagged.tiff'
            run(['convert','-size','1x1','-depth','8',mode.lower()+':-','-profile',str(profile),str(src)],pixel)
            source=openp(str(profile).encode(),b'r');dest=openp(str(PROFILES/'srgb.icc').encode(),b'r')
            transform=create(source,((6 if mode=='CMYK' else 4)<<16)|(len(pixel)<<3)|1,dest,(4<<16)|(3<<3)|1,0,0)
            self.assertTrue(transform)
            expected=(C.c_ubyte*3)();do(transform,C.create_string_buffer(pixel),expected,1)
            delete(transform);close(source);close(dest)
            for stripped in (False,True):
                out=self.convert(src,strip=stripped)
                actual=run(['convert',str(out),'-depth','8','rgb:-'])
                self.assertTrue(all(abs(a-b)<=2 for a,b in zip(actual,expected)),(profile,stripped,list(actual),list(expected)))
            animated=self.work/'managed.webp'
            sp.run(utils.build_command([str(src),str(src)],str(animated),'webp',10,0,None,None,100),check=True,stdout=sp.PIPE,stderr=sp.PIPE)
            actual=run(['convert',str(animated)+'[0]','-depth','8','rgb:-'])
            self.assertTrue(all(abs(a-b)<=4 for a,b in zip(actual,expected)),(profile,'animated WebP',list(actual),list(expected)))
    def test_alpha_preserved_in_png_and_white_for_jpeg(self):
        src=self.work/'alpha.png';run(['convert','-size','32x32','xc:rgba(255,0,0,0.5)','PNG32:'+str(src)])
        out=self.convert(src);rgba=run(['convert',str(out),'-depth','8','rgba:-'])
        self.assertLessEqual(abs(rgba[3]-128),1)
        gray=self.convert(src,grayscale=True)
        self.assertIn(main._output_color_metadata(gray)['colorspace'],('sRGB','Gray'))
        neutral=run(['convert',str(gray),'-depth','8','rgb:-'])
        self.assertEqual(neutral[0],neutral[1]);self.assertEqual(neutral[1],neutral[2])
        out=self.convert(src,'jpg');rgb=run(['convert',str(out),'-depth','8','rgb:-'])
        self.assertTrue(rgb[0]>250 and 120<=rgb[1]<=135 and 120<=rgb[2]<=135,list(rgb[:3]))
    def test_16_bit_png_and_tiff_keep_more_than_256_levels(self):
        src=self.work/'deep.png';run(['convert','-size','32x1024','gradient:black-white','-rotate','90','-depth','16',str(src)])
        for fmt in ('png','tiff'):
            out=self.convert(src,fmt)
            raw=run(['convert',str(out),'-depth','16','-endian','MSB','gray:-'])
            self.assertGreater(len(set(struct.unpack('>'+str(len(raw)//2)+'H',raw))),256)
        prepared=self.convert(src,'png');assembled=self.work/'pages.tiff'
        sp.run(utils.build_assembly_command([str(prepared),str(prepared)],str(assembled),'tiff',10,0,100),check=True,stdout=sp.PIPE,stderr=sp.PIPE)
        pages=run(['identify','-format','%z\n',str(assembled)]).decode().splitlines()
        self.assertEqual(pages,['16','16'])
        self.assertTrue(run(['convert',str(assembled)+'[0]','-depth','16','gray:-']))
    def test_hdr_video_metadata_is_rejected_before_decode(self):
        src=self.work/'pq.mp4'
        run(['ffmpeg','-v','error','-f','lavfi','-i','color=black:s=16x16','-frames:v','1','-c:v','libx264','-color_trc','smpte2084',str(src)])
        with self.assertRaises(main.HTTPException) as error:main._reject_unsupported_color(src)
        self.assertEqual(error.exception.status_code,400)
    def test_apng_preserves_and_normalizes_srgb_and_other_icc(self):
        import zlib
        src=self.work/'animation.apng'
        run(['ffmpeg','-v','error','-f','lavfi','-i','color=0xb4501e:s=16x16:r=10','-frames:v','2','-pix_fmt','rgba','-f','apng',str(src)])
        original=src.read_bytes()
        for name,rejected in [('srgb.icc',False),('a98.icc',True)]:
            payload=b'ICC\0\0'+zlib.compress((PROFILES/name).read_bytes())
            chunk=struct.pack('>I',len(payload))+b'iCCP'+payload+struct.pack('>I',zlib.crc32(b'iCCP'+payload)&0xffffffff)
            src.write_bytes(original[:33]+chunk+original[33:])
            main._reject_unsupported_color(src)
            self.assertEqual(main._probe_frames_and_size(src)[0],2)
            self.assertEqual(main._read_frame_delays(str(src)),[10,10])
            frames=main._extract_frames(src,self.work,{},prefix=name.split('.')[0])
            self.assertEqual(len(frames),2,(name,utils.is_apng(src),run(['ffprobe','-v','error','-count_frames','-show_entries','stream=nb_read_frames','-of','json',str(src)])))
            self.assertEqual(main._output_color_metadata(Path(frames[0]))['colorspace'],'sRGB')
            for frame in frames:
                red=run(['convert',frame,'-depth','8','rgb:-'])[0]
                self.assertTrue(red>195 if rejected else 165<red<195,(name,red))
    def test_hdr_png_cicp_is_rejected(self):
        import zlib
        src=self.work/'hdr.png';run(['convert','-size','1x1','xc:red',str(src)])
        original=src.read_bytes();payload=bytes([9,16,0,1])
        chunk=struct.pack('>I',4)+b'cICP'+payload+struct.pack('>I',zlib.crc32(b'cICP'+payload)&0xffffffff)
        src.write_bytes(original[:33]+chunk+original[33:])
        with self.assertRaises(main.HTTPException) as error:main._reject_unsupported_color(src)
        self.assertEqual(error.exception.status_code,400)
    def test_video_color_tags_and_jpeg_extraction_background_are_explicit(self):
        for i in range(2):run(['convert','-size','16x16','xc:rgba(255,0,0,0.5)','PNG32:'+str(self.work/f'f{i:05d}.png')])
        for fmt in ('mp4','webm'):
            out=self.work/('video.'+fmt)
            sp.run(utils.build_video_encode_command(str(self.work/'f%05d.png'),str(out),10,fmt),check=True,stdout=sp.PIPE,stderr=sp.PIPE)
            info=main._output_color_metadata(out)
            self.assertEqual(info['color_space'],'bt709')
            self.assertEqual(info['color_primaries'],'bt709')
            self.assertEqual(info['color_transfer'],'iec61966-2-1')
            self.assertEqual(info['color_range'],'tv')
        output=self.work/'extract_%05d.jpg'
        sp.run(utils.build_explode_command(str(self.work/'f00000.png'),str(output),'jpg',100),check=True,stdout=sp.PIPE,stderr=sp.PIPE)
        rgb=run(['convert',str(self.work/'extract_00000.jpg'),'-depth','8','rgb:-'])
        self.assertTrue(rgb[0]>250 and 120<=rgb[1]<=135)
    def test_alpha_gradient_and_recorded_processing_metadata(self):
        src=self.work/'gradient.png'
        pixels=b''.join(bytes((255,0,0,a)) for a in range(256))*16
        run(['convert','-size','256x16','-depth','8','rgba:-','PNG32:'+str(src)],pixels)
        for fmt in ('png','webp'):
            out=self.convert(src,fmt)
            rgba=run(['convert',str(out),'-depth','8','rgba:-'])
            self.assertEqual(list(rgba[3:1024:4]),list(range(256)))
        shutil.copyfile(src,self.work/'a00000.png');shutil.copyfile(src,self.work/'a00001.png')
        apng=self.work/'alpha.apng'
        sp.run(utils.build_apng_encode_command(str(self.work/'a%05d.png'),str(apng),10,0),check=True,stdout=sp.PIPE,stderr=sp.PIPE)
        raw=run(['ffmpeg','-v','error','-i',str(apng),'-frames:v','1','-f','rawvideo','-pix_fmt','rgba','-'])
        self.assertEqual(list(raw[3:1024:4]),list(range(256)))
        name='fidelity-policy.png';main.OUTPUT_DIR.mkdir(parents=True,exist_ok=True)
        output=main.OUTPUT_DIR/name;recipe=main.RECIPE_DIR/(name+'.json')
        try:
            shutil.copyfile(self.convert(src),output)
            main._write_recipe(name,'convert-image',{'format':'png','quality':100},['gradient.png'],1,256,16)
            effective=json.loads(recipe.read_text())['effective']
            self.assertEqual(effective['processing_policy'],'sdr-srgb-v1')
            self.assertEqual(effective['color']['depth'],8)
            self.assertEqual(effective['color']['colorspace'],'sRGB')
        finally:
            output.unlink(missing_ok=True);recipe.unlink(missing_ok=True)

    def test_sdr_video_transfer_is_converted_to_srgb_without_odd_size_loss(self):
        src=self.work/'bt709.mp4'
        run(['ffmpeg','-v','error','-f','lavfi','-i','color=gray:s=16x16','-frames:v','1','-c:v','libx264','-color_trc','bt709','-color_primaries','bt709','-colorspace','bt709',str(src)])
        before=run(['ffmpeg','-v','error','-i',str(src),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','-'])[0]/255
        linear=before/4.5 if before<.081 else ((before+.099)/1.099)**(1/.45)
        expected=round(255*(12.92*linear if linear<=.0031308 else 1.055*linear**(1/2.4)-.055))
        frames=main._extract_frames(src,self.work,{},prefix='video')
        actual=run(['convert',frames[0],'-depth','8','rgb:-'])[0]
        self.assertLessEqual(abs(actual-expected),3,(actual,expected))
        odd=self.work/'odd.webm'
        run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc=size=17x19:rate=1','-frames:v','1','-c:v','libvpx-vp9','-pix_fmt','yuv444p',str(odd)])
        frames=main._extract_frames(odd,self.work,{},prefix='odd')
        self.assertEqual(main._read_output_dims(Path(frames[0])),(17,19))

        base=self.work/'base.mp4';rotated=self.work/'rotated.mp4'
        run(['ffmpeg','-v','error','-f','lavfi','-i','color=red:s=16x32','-frames:v','1','-c:v','libx264',str(base)])
        run(['ffmpeg','-v','error','-i',str(base),'-c','copy','-metadata:s:v:0','rotate=90',str(rotated)])
        frames=main._extract_frames(rotated,self.work,{},prefix='rotated')
        self.assertEqual(main._read_output_dims(Path(frames[0])),(32,16))
