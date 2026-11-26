/* Managed, root-only GLES producer file overlay. No process injection.
 *
 * Usage: xenoid-renderer {status|bind|detach} SOURCE SHA SOURCE_DEV SOURCE_INO
 *                       BASE_DEV BASE_INO
 * The caller pins the original inode before installation and journals it.
 * This helper independently hashes both files and rejects an unrelated mount.
 * /proc/mounts may be a profile view, so mount identity comes from open/stat,
 * exact content and readonly filesystem flags, never shell mount output.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/types.h>
#include <sys/xattr.h>
#include <unistd.h>

#ifndef UMOUNT_NOFOLLOW
#define UMOUNT_NOFOLLOW 8
#endif
#ifndef XENOID_RENDERER_TARGET
#define XENOID_RENDERER_TARGET "/vendor/lib64/egl/libGLESv2_angle.so"
#endif
#ifndef XENOID_RENDERER_ROOT
#define XENOID_RENDERER_ROOT "/data/local/tmp/xenoid-renderer"
#endif
#define BASE_SHA "8385182775899793a5831cd9535b07b8d993b8582e4c4bbbf58d06e828f5216d"
#define MAX_DRIVER_SIZE (9U * 1024U * 1024U)

/* Small SHA-256 implementation; no dependency on Android private crypto ABIs. */
typedef struct { uint32_t h[8]; uint64_t bytes; size_t used; unsigned char b[64]; } sha256;
static uint32_t rr(uint32_t x, unsigned int n) { return (x >> n) | (x << (32 - n)); }
static void sha_block(sha256 *s, const unsigned char *b) {
  static const uint32_t k[64] = {
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
  uint32_t w[64],a,c,d,e,f,g,h,t1,t2,bb;
  for (int i=0;i<16;i++) w[i]=(uint32_t)b[i*4]<<24|(uint32_t)b[i*4+1]<<16|(uint32_t)b[i*4+2]<<8|b[i*4+3];
  for (int i=16;i<64;i++) w[i]=w[i-16]+(rr(w[i-15],7)^rr(w[i-15],18)^(w[i-15]>>3))+w[i-7]+(rr(w[i-2],17)^rr(w[i-2],19)^(w[i-2]>>10));
  a=s->h[0];bb=s->h[1];c=s->h[2];d=s->h[3];e=s->h[4];f=s->h[5];g=s->h[6];h=s->h[7];
  for(int i=0;i<64;i++) {
    t1=h+(rr(e,6)^rr(e,11)^rr(e,25))+((e&f)^(~e&g))+k[i]+w[i];
    t2=(rr(a,2)^rr(a,13)^rr(a,22))+((a&bb)^(a&c)^(bb&c));
    h=g;g=f;f=e;e=d+t1;d=c;c=bb;bb=a;a=t1+t2;
  }
  s->h[0]+=a;s->h[1]+=bb;s->h[2]+=c;s->h[3]+=d;s->h[4]+=e;s->h[5]+=f;s->h[6]+=g;s->h[7]+=h;
}
static void sha_init(sha256 *s) {
  static const uint32_t initial[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
  memset(s,0,sizeof(*s));memcpy(s->h,initial,sizeof(initial));
}
static void sha_update(sha256 *s, const unsigned char *b, size_t n) {
  s->bytes+=n;
  while(n) { size_t take=64-s->used;if(take>n)take=n;memcpy(s->b+s->used,b,take);s->used+=take;b+=take;n-=take;
    if(s->used==64){sha_block(s,s->b);s->used=0;}}
}
static void sha_final(sha256 *s,char hex[65]) {
  uint64_t bits=s->bytes*8;s->b[s->used++]=0x80;
  if(s->used>56){memset(s->b+s->used,0,64-s->used);sha_block(s,s->b);s->used=0;}
  memset(s->b+s->used,0,56-s->used);for(int i=0;i<8;i++)s->b[63-i]=(unsigned char)(bits>>(8*i));sha_block(s,s->b);
  for(int i=0;i<8;i++)snprintf(hex+i*8,9,"%08" PRIx32,s->h[i]);
  hex[64]=0;
}

static int error(const char *code) {
  printf("{\"schema\":\"dev.xenoid.renderer-mount/v1\",\"ok\":false,\"error\":\"%s\",\"errno\":%d}\n",code,errno);
  return 2;
}
static int decimal(const char *s,uint64_t *v) {
  if(!s||!*s)return -1;
  for(const char *p=s;*p;p++)if(*p<'0'||*p>'9')return -1;
  errno=0;char *end=NULL;unsigned long long n=strtoull(s,&end,10);
  if(errno||!end||*end||n==0)return -1;
  *v=(uint64_t)n;return 0;
}
static int valid_sha(const char *s) {
  if(!s||strlen(s)!=64)return 0;
  for(int i=0;i<64;i++)if(!((s[i]>='0'&&s[i]<='9')||(s[i]>='a'&&s[i]<='f')))return 0;
  return 1;
}
static int safe_source(const char *path,const char *sha) {
  char expected[PATH_MAX];
  int n=snprintf(expected,sizeof(expected),XENOID_RENDERER_ROOT "/objects/%s/libGLESv2_angle.so",sha);
  if(n<0||(size_t)n>=sizeof(expected)||strcmp(path,expected))return -1;
  char current[PATH_MAX];
  if(strlen(path)>=sizeof(current))return -1;
  memcpy(current,path,strlen(path)+1);
  for(char *p=current+1;*p;p++) {
    if(*p!='/')continue;
    *p=0;struct stat st;int rc=lstat(current,&st);*p='/';
    if(rc||!S_ISDIR(st.st_mode))return -1;
    /* Android /data/local/tmp is 0771 and root-owned; the private stage must
       prevent app traversal and all descendants must retain that boundary. */
    if((size_t)(p-current)>=strlen(XENOID_RENDERER_ROOT)
       &&(st.st_uid!=0||(st.st_mode&0077)))return -1;
  }
  return 0;
}
static int identity(const struct stat *s,uint64_t dev,uint64_t ino) {
  return (uint64_t)s->st_dev==dev&&(uint64_t)s->st_ino==ino;
}
static int regular_stat(const char *path,struct stat *s) {
  return lstat(path,s)==0&&S_ISREG(s->st_mode)&&s->st_uid==0&&s->st_gid==0
    &&s->st_nlink==1&&(s->st_mode&07777)==0644&&s->st_size>0&&s->st_size<=MAX_DRIVER_SIZE;
}
static int hash_file(const char *path,const struct stat *expected,char hex[65]) {
  int fd=open(path,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(fd<0)return -1;
  struct stat before,after;if(fstat(fd,&before)||before.st_dev!=expected->st_dev||before.st_ino!=expected->st_ino){close(fd);return -1;}
  sha256 s;sha_init(&s);unsigned char b[16384];off_t total=0;
  for(;;){ssize_t n=read(fd,b,sizeof(b));if(n<0&&errno==EINTR)continue;if(n<0){close(fd);return -1;}if(!n)break;
    total+=n;if(total>MAX_DRIVER_SIZE){close(fd);return -1;}sha_update(&s,b,(size_t)n);}
  int rc=fstat(fd,&after);close(fd);
  if(rc||total!=before.st_size||before.st_size!=after.st_size||before.st_mtime!=after.st_mtime
     ||before.st_ctime!=after.st_ctime)return -1;
  sha_final(&s,hex);return 0;
}
static int readonly(const char *path) {
  struct statvfs fs;return statvfs(path,&fs)==0&&(fs.f_flag&ST_RDONLY);
}
static int matching_context(const char *a,const char *b) {
#if defined(__APPLE__)
  (void)a;(void)b;return 1; /* Host tests do not make Android mounts. */
#else
  char aa[256],bb[256];errno=0;ssize_t x=getxattr(a,"security.selinux",aa,sizeof(aa));int ea=errno;
  errno=0;ssize_t y=getxattr(b,"security.selinux",bb,sizeof(bb));int eb=errno;
  if(x<0&&y<0&&(ea==ENODATA||ea==ENOTSUP)&&(eb==ENODATA||eb==ENOTSUP))return 1;
  return x>=0&&x==y&&!memcmp(aa,bb,(size_t)x);
#endif
}
static int inspect(const char *source,const char *sha,uint64_t sd,uint64_t si,uint64_t bd,uint64_t bi,
                   struct stat *src,struct stat *target) {
  if(safe_source(source,sha)||!regular_stat(source,src)||!identity(src,sd,si))return error("source_identity_invalid");
  char hash[65];if(hash_file(source,src,hash)||strcmp(hash,sha))return error("source_digest_invalid");
  if(!regular_stat(XENOID_RENDERER_TARGET,target))return error("target_metadata_invalid");
  if(hash_file(XENOID_RENDERER_TARGET,target,hash))return error("target_unreadable");
  if(identity(target,sd,si)) {
    /* A failed remount must remain detachable by its exact owned inode. */
    if(strcmp(hash,sha))return error("bound_target_invalid");
    return 1;
  }
  if(!identity(target,bd,bi)||strcmp(hash,BASE_SHA)||!readonly(XENOID_RENDERER_TARGET))return error("unowned_target");
  if(!matching_context(source,XENOID_RENDERER_TARGET))return error("selinux_context_mismatch");
  return 0;
}
static int success(const char *state,int changed,const struct stat *s) {
  printf("{\"schema\":\"dev.xenoid.renderer-mount/v1\",\"ok\":true,\"state\":\"%s\",\"changed\":%s,\"readonly\":true,\"device\":%" PRIu64 ",\"inode\":%" PRIu64 "}\n",
         state,changed?"true":"false",(uint64_t)s->st_dev,(uint64_t)s->st_ino);
  return 0;
}
int main(int argc,char **argv) {
  if(argc!=8||(!strcmp(argv[1],"status")?0:strcmp(argv[1],"bind")&&strcmp(argv[1],"detach")))return error("arguments_invalid");
  if(geteuid()!=0)return error("root_required");
  uint64_t sd,si,bd,bi;
  if(!valid_sha(argv[3])||decimal(argv[4],&sd)||decimal(argv[5],&si)||decimal(argv[6],&bd)||decimal(argv[7],&bi)
     ||(sd==bd&&si==bi)||!strcmp(argv[3],BASE_SHA))return error("expected_identity_invalid");
  struct stat src,target;int state=inspect(argv[2],argv[3],sd,si,bd,bi,&src,&target);
  if(state>1)return state;
  if(!strcmp(argv[1],"status")) {
    if(!readonly(XENOID_RENDERER_TARGET))return error("target_not_readonly");
    return success(state?"bound":"native",0,&target);
  }
  if(!strcmp(argv[1],"detach")) {
    if(!state)return success("native",0,&target);
    if(umount2(XENOID_RENDERER_TARGET,MNT_DETACH|UMOUNT_NOFOLLOW))return error("detach_failed");
    state=inspect(argv[2],argv[3],sd,si,bd,bi,&src,&target);
    if(state!=0)return state>1?state:error("original_not_restored");
    return success("native",1,&target);
  }
  if(state&&readonly(XENOID_RENDERER_TARGET))return success("bound",0,&target);
  if(!state&&mount(argv[2],XENOID_RENDERER_TARGET,NULL,MS_BIND,NULL))return error("bind_failed");
  if(mount(NULL,XENOID_RENDERER_TARGET,NULL,MS_PRIVATE,NULL)
     ||mount(NULL,XENOID_RENDERER_TARGET,NULL,MS_BIND|MS_REMOUNT|MS_RDONLY,NULL)) {
    int saved=errno;int detached=umount2(XENOID_RENDERER_TARGET,MNT_DETACH|UMOUNT_NOFOLLOW);
    errno=saved;return error(detached?"bind_rollback_failed":"bind_setup_failed_restored");
  }
  state=inspect(argv[2],argv[3],sd,si,bd,bi,&src,&target);
  if(state!=1||!readonly(XENOID_RENDERER_TARGET)) {
    /* Detach only if stat still proves this operation's exact source inode. */
    struct stat now;
    if(lstat(XENOID_RENDERER_TARGET,&now)==0&&identity(&now,sd,si))
      (void)umount2(XENOID_RENDERER_TARGET,MNT_DETACH|UMOUNT_NOFOLLOW);
    return state>1?state:error("bind_readback_failed");
  }
  return success("bound",1,&target);
}
