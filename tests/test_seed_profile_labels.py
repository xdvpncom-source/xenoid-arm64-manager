"""Actual seed classifier/publication tests; Image supplies only stored fixtures."""
import copy,hashlib,importlib.util,json,re,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('seed_label_fixture',ROOT/'nativebundle/source/scripts/seed-cold-profile.py')
seed=importlib.util.module_from_spec(spec);spec.loader.exec_module(seed)
TEMPLATE=ROOT/'nativebundle/source/examples/fingerprints/pixel-bluejay-android13.json'
POLICY=b'/data/local/tmp(/.*)? u:object_r:shell_data_file:s0\n'
D='/local/tmp/xenoid-profile/'
def profile():
 p=json.loads(TEMPLATE.read_bytes());p['ids']={'android_id':'0123456789abcdef','boot_id':'11111111-2222-4333-8444-555555555555','serial':'TEST000000000001','imei':'123456789012345','imeisv':'01'};p['usb']['serial']=p['ids']['serial'];return p
class Image:
 def __init__(self,p):
  self.values={D+k:v for k,v in seed.profile_leaves(p).items()};self.meta={};self.writes=[]
  for path,(uid,gid,mode,label)in seed.LABELS.items():self.meta[path]=[uid,gid,mode,label.encode()+b'\0',1,True]
  for path in self.values:self.meta[path]=[0,0,0o600 if path.rsplit('/',1)[1]in('hostname','device_name','bluetooth_address')else 0o644,seed.FILE_LABEL.encode()+b'\0',1,False]
 def names(self,path):return {'lost+found','local','system'}if path=='/'else{'tmp'}if path=='/local'else{'xenoid-profile'}
 def exists(self,path):return path in self.meta
 def read(self,path):return self.values[path]
 def info(self,path):
  m=self.meta[path];return 'Type: '+('directory'if m[5]else'regular')+'\nLinks: '+str(m[4])
 def ownership_matches(self,path,uid,gid,mode):return self.meta[path][:3]==[uid,gid,mode]
 def label(self,path):return self.meta[path][3]
 def metadata_matches(self,path,uid,gid,mode,label):return self.ownership_matches(path,uid,gid,mode)and self.label(path)==label.encode()+b'\0'
 def set_label(self,path,label):self.writes.append(path);self.meta[path][3]=label.encode()+b'\0'
class Labels(unittest.TestCase):
 def setUp(self):self.p=profile();self.i=Image(self.p)
 def missing(self,*names):
  for n in names:self.i.meta[D+n][3]=None
 def run_seed(self):return seed.seed_image(self.i,self.p,policy=POLICY)
 def test_actual_two_missing_labels_restored_only_after_full_validation(self):
  self.missing('statfs_fsid','hostname');old=copy.deepcopy(self.i.values);modes={k:v[:3]for k,v in self.i.meta.items()}
  r=self.run_seed();self.assertEqual(r['labelsRepaired'],2);self.assertEqual(self.i.writes,[D+'statfs_fsid',D+'hostname']);self.assertEqual(old,self.i.values);self.assertEqual(modes,{k:v[:3]for k,v in self.i.meta.items()});self.assertEqual(self.run_seed()['labelsRepaired'],0)
 def test_wrong_nonempty_label_rejects_without_repair(self):
  for bad in (b'u:object_r:system_data_file:s0\0',b'\0',b'wrong'):
   with self.subTest(bad=bad):
    self.setUp();self.missing('statfs_fsid');self.i.meta[D+'hostname'][3]=bad
    with self.assertRaisesRegex(seed.SeedError,'profile_metadata_mismatch'):self.run_seed()
    self.assertEqual(self.i.writes,[])
 def test_wrong_uid_gid_modes_or_links_reject_missing_label(self):
  for field,value in ((0,2000),(1,2000),(2,0o644),(4,2)):
   with self.subTest(field=field):
    self.setUp();self.missing('statfs_fsid','hostname');self.i.meta[D+'hostname'][field]=value
    with self.assertRaisesRegex(seed.SeedError,'profile_metadata_mismatch'):self.run_seed()
    self.assertEqual(self.i.writes,[])
 def test_wrong_fsid_mode_rejects(self):
  self.missing('statfs_fsid');self.i.meta[D+'statfs_fsid'][2]=0o600
  with self.assertRaisesRegex(seed.SeedError,'profile_metadata_mismatch'):self.run_seed()
  self.assertEqual(self.i.writes,[])
 def test_late_invalid_value_does_not_partially_repair_earlier_fsid(self):
  self.missing('statfs_fsid','hostname');self.i.values[D+'storage_filesystem']=b'not-the-profile\n'
  with self.assertRaisesRegex(seed.SeedError,'profile_seed_conflict'):self.run_seed()
  self.assertEqual(self.i.writes,[]);self.assertIsNone(self.i.label(D+'statfs_fsid'))
 def test_invalid_mutable_values_reject_without_repair(self):
  for name,value in (('statfs_fsid',b'0000000000000000\n'),('hostname',b'bad host\n')):
   with self.subTest(name=name):
    self.setUp();self.missing('statfs_fsid','hostname');self.i.values[D+name]=value
    with self.assertRaisesRegex(seed.SeedError,'profile_seed_conflict'):self.run_seed()
    self.assertEqual(self.i.writes,[])
 def test_existing_GEN_owned_valid_values_preserved(self):
  self.i.values[D+'statfs_fsid']=b'abcdef0123456789\n';self.i.values[D+'hostname']=b'android-current-owned\n';self.i.values[D+'device_name']=b'Pixel 6a OWNED123\n';self.i.values[D+'bluetooth_address']=b'02:12:34:56:78:9A\n';self.missing('statfs_fsid','hostname');before=copy.deepcopy(self.i.values)
  self.assertEqual(self.run_seed()['labelsRepaired'],2);self.assertEqual(self.i.values,before)
 def test_other_identity_missing_label_remains_forbidden(self):
  self.missing('statfs_fsid','serial')
  with self.assertRaisesRegex(seed.SeedError,'profile_metadata_mismatch'):self.run_seed()
  self.assertEqual(self.i.writes,[])
 def test_profile_conflict_rejects_before_any_label_write(self):
  self.missing('statfs_fsid','hostname');self.p['memory']['totalBytes']+=1
  with self.assertRaisesRegex(seed.SeedError,'profile_seed_conflict'):self.run_seed()
  self.assertEqual(self.i.writes,[])
 def test_old_nonunique_repair_retained(self):
  self.missing('cpu_count','battery_level');self.assertEqual(self.run_seed()['labelsRepaired'],2)
if __name__=='__main__':unittest.main()
