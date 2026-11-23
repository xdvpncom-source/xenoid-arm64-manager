"""Runtime-free tests for one profile-owned metadata producer."""
from copy import deepcopy
import importlib.util,json,subprocess,tempfile,unittest
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
CANDIDATE=ROOT
sys.path.insert(0,str(ROOT/'src'))
from xenoid import cold_profile as new
spec=importlib.util.spec_from_file_location('profile_props',ROOT/'profiles/runtime-v1/scripts/patch-runtime-props.py')
props=importlib.util.module_from_spec(spec);spec.loader.exec_module(props)

class MetadataTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.temp=tempfile.TemporaryDirectory();d=Path(cls.temp.name);cls.d=d
  java=CANDIDATE/'daemon/app/src/main/java/dev/xenoid/daemon/BuildMetadata.java'
  harness=d/'MetadataMain.java'
  harness.write_text('''package dev.xenoid.daemon;
import java.util.*;
public class MetadataMain { public static void main(String[] args) {
 Map<String,Object> data=new LinkedHashMap<>();
 for(String arg:args){int eq=arg.indexOf('='); data.put(arg.substring(0,eq),arg.substring(eq+1));}
 try { for(Map.Entry<String,String> e:BuildMetadata.properties(data).entrySet())System.out.println(e.getKey()+"="+e.getValue()); }
 catch(IllegalArgumentException e){System.exit(2);}
}}''')
  subprocess.run(['javac','-d',str(d),str(java),str(harness)],check=True,capture_output=True)
  src=(CANDIDATE/'native/xenoid-ril/xenoid_ril.c').read_text();start=src.index('static int baseband_version(');end=src.index('\nstatic void response_device_identity',start)
  c=d/'baseband-test.c';c.write_text('''#include <stdio.h>
#include <string.h>
#define PROP_VALUE_MAX 92
static const char *test_value;
static int __system_property_get(const char *key,char *out){
 if(strcmp(key,"ro.build.expect.baseband"))return -1;
 if(!test_value){out[0]=0;return 0;}
 snprintf(out,PROP_VALUE_MAX,"%s",test_value);return (int)strlen(out);
}
'''+src[start:end]+'''\nint main(int argc,char **argv){char out[PROP_VALUE_MAX];test_value=argc>1?argv[1]:NULL;
 if(baseband_version(out)) { return 2; }
 puts(out);
 return 0;
}
''')
  subprocess.run(['cc','-std=c11','-Wall','-Werror',str(c),'-o',str(d/'baseband-test')],check=True,capture_output=True)
 @classmethod
 def tearDownClass(cls):cls.temp.cleanup()
 def java(self,values):
  r=subprocess.run(['java','-cp',str(self.d),'dev.xenoid.daemon.MetadataMain',*[k+'='+v for k,v in values.items()]],capture_output=True,text=True)
  return r.returncode,dict(line.split('=',1)for line in r.stdout.splitlines())
 def test_absent_metadata_preserves_legacy(self):
  self.assertEqual(new.build_metadata_properties({}),{})
  profile=json.loads((ROOT/'examples/fingerprints/pixel-bluejay-android13.json').read_text())
  for key in ('host','user','date_utc','baseband'):profile['build'].pop(key,None)
  expected=new.expected_properties(profile)
  self.assertFalse(any(k in expected for k in ('ro.build.host','ro.build.user','ro.build.date.utc','ro.build.expect.baseband','gsm.version.baseband')))
  generated=props.generated_partition_lines(profile['build']);before=deepcopy(generated)
  new.augment_partition_lines(generated,None);self.assertEqual(generated,before)
 def test_verified_baseband_independent_of_unverified_fields(self):
  values={'baseband':'g5123b-102852-220720-B-8851166'}
  self.assertEqual(new.build_metadata_properties(values),{'ro.build.expect.baseband':values['baseband'],'gsm.version.baseband':values['baseband']})
 def test_python_java_property_parity(self):
  for fields in [{},{'baseband':'g5123b-102852-220720-B-8851166'},
    {'host':'test-host','user':'test-user','date_utc':'0'},
    {'host':'test-host','user':'test-user','date_utc':'1662059754','baseband':'test.1_2-3'}]:
   with self.subTest(fields=fields):
    rc,actual=self.java(fields);self.assertEqual(rc,0);self.assertEqual(actual,new.build_metadata_properties(fields))
 def test_partition_dates_derived_from_one_epoch(self):
  actual=new.build_metadata_properties({'date_utc':'1662059754'})
  self.assertEqual(len(actual),20)
  self.assertEqual({v for k,v in actual.items()if k.endswith('.date')},{'Thu Sep  1 19:15:54 UTC 2022'})
  self.assertEqual({v for k,v in actual.items()if k.endswith('.utc')},{'1662059754'})
 def test_invalid_metadata_rejected_by_both_adapters(self):
  for field,value in [('host','a\nb'),('user','../user'),('baseband','bad value'),('date_utc','-1'),('date_utc','1.5'),('date_utc','1e3'),('host','x'*92)]:
   with self.subTest(field=field,value=value):
    with self.assertRaises(ValueError):new.build_metadata_properties({field:value})
    self.assertEqual(self.java({field:value})[0],2)
 def test_nonstrings_rejected(self):
  for field in ['host','user','baseband','date_utc']:
   for value in [None,True,123,{},[]]:
    with self.subTest(field=field,value=value):
     with self.assertRaises(ValueError):new.build_metadata_properties({field:value})
 def test_official_apply_validates_new_fields_before_first_mutation(self):
  source=(CANDIDATE/'daemon/app/src/main/java/dev/xenoid/daemon/DeviceProfileManager.java').read_text()
  self.assertLess(source.index('validatedMetadata = BuildMetadata.properties'),source.index('actions.add(applyField('))
 def test_generated_properties_all_from_same_metadata(self):
  profile=json.loads((ROOT/'examples/fingerprints/pixel-bluejay-android13.json').read_text());before=deepcopy(profile)
  profile['build'].update(host='test-host',user='test-user',date_utc='1662059754',baseband='test-baseband')
  generated=props.generated_partition_lines(profile['build']);new.augment_partition_lines(generated,profile)
  expected=new.build_metadata_properties(profile['build'])
  system=dict(line.split('=',1)for line in generated['system_build.prop'])
  self.assertTrue(all(system.get(k)==v for k,v in expected.items()))
  for name,lines in generated.items():
   scope=name.removesuffix('_build.prop');values=dict(line.split('=',1)for line in lines)
   self.assertEqual(values['ro.'+scope+'.build.date.utc'],'1662059754')
  for key in new.RUNTIME_FIELDS:self.assertEqual(profile['build'][key],before['build'][key])
  self.assertEqual(profile['ids'],before['ids']);self.assertEqual(profile['display'],before['display'])
 def test_actual_ril_helper_uses_profile_property(self):
  val='g5123b-102852-220720-B-8851166'
  r=subprocess.run([str(self.d/'baseband-test'),val],capture_output=True,text=True)
  self.assertEqual((r.returncode,r.stdout.strip()),(0,val))
 def test_ril_legacy_default_unchanged(self):
  r=subprocess.run([str(self.d/'baseband-test')],capture_output=True,text=True)
  self.assertEqual((r.returncode,r.stdout.strip()),(0,'g5300g-230605-230621-B-10346107'))
 def test_invalid_ril_property_not_returned(self):
  r=subprocess.run([str(self.d/'baseband-test'),'bad value'],capture_output=True,text=True)
  self.assertEqual(r.returncode,2);self.assertEqual(r.stdout,'')
if __name__=='__main__':unittest.main()
