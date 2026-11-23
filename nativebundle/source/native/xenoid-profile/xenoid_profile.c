// xenoid-profile: native staged profile reader for future system/service hooks.
// It reads /data/local/tmp/xenoid-profile/effective.json and helper field files
// produced by Xenoid daemon. This provides one native ABI-stable interface for
// later LD_PRELOAD/Zygisk/system_server modules.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/file.h>
#include <sys/xattr.h>
#include <limits.h>
#include <unistd.h>

#ifndef PROFILE_DIR
#define PROFILE_DIR "/data/local/tmp/xenoid-profile"
#endif
#define EFFECTIVE PROFILE_DIR "/effective.json"
#define BOOT_ID PROFILE_DIR "/boot_id"

static char *read_file(const char *path) {
  int fd = open(path, O_RDONLY);
  if (fd < 0) return NULL;
  struct stat st;
  if (fstat(fd, &st) != 0) { close(fd); return NULL; }
  size_t cap = st.st_size > 0 ? (size_t)st.st_size : 4096;
  char *buf = (char *)calloc(1, cap + 1);
  if (!buf) { close(fd); return NULL; }
  ssize_t n = read(fd, buf, cap);
  close(fd);
  if (n < 0) { free(buf); return NULL; }
  buf[n] = 0;
  return buf;
}

static void print_json_string(const char *s) {
  putchar('"');
  if (s) for (const char *p=s; *p; ++p) {
    if (*p == '\\' || *p == '"') { putchar('\\'); putchar(*p); }
    else if (*p == '\n') printf("\\n");
    else if (*p == '\r') printf("\\r");
    else putchar(*p);
  }
  putchar('"');
}

static char *extract_string(const char *json, const char *key) {
  if (!json || !key) return NULL;
  char needle[128];
  snprintf(needle, sizeof(needle), "\"%s\"", key);
  char *p = strstr((char *)json, needle);
  if (!p) return NULL;
  p = strchr(p + strlen(needle), ':');
  if (!p) return NULL;
  p++;
  while (*p == ' ' || *p == '\t' || *p == '\n') p++;
  if (*p != '"') return NULL;
  p++;
  char *start = p;
  while (*p && !(*p == '"' && *(p-1) != '\\')) p++;
  size_t len = (size_t)(p - start);
  char *out = (char *)calloc(1, len + 1);
  if (!out) return NULL;
  memcpy(out, start, len);
  return out;
}

static int status(void) {
  printf("{\"ok\":true,\"schema\":\"dev.xenoid.profile.native/v1\",");
  printf("\"profilePath\":\"%s\",\"profileExists\":%s,", EFFECTIVE, access(EFFECTIVE, F_OK)==0 ? "true" : "false");
  printf("\"bootIdPath\":\"%s\",\"bootIdExists\":%s}\n", BOOT_ID, access(BOOT_ID, F_OK)==0 ? "true" : "false");
  return 0;
}

static int dump(void) {
  char *j = read_file(EFFECTIVE);
  if (!j) { printf("{\"ok\":false,\"error\":\"profile not found\",\"path\":\"%s\"}\n", EFFECTIVE); return 1; }
  printf("%s\n", j);
  free(j);
  return 0;
}

static int get_key(const char *key) {
  if (!strcmp(key, "boot_id")) {
    char *b = read_file(BOOT_ID);
    if (b) { printf("%s\n", b); free(b); return 0; }
  }
  char *j = read_file(EFFECTIVE);
  if (!j) return 1;
  char *v = extract_string(j, key);
  if (!v) { free(j); return 2; }
  printf("%s\n", v);
  free(v); free(j);
  return 0;
}

static int env(void) {
  char *j = read_file(EFFECTIVE);
  char *boot = read_file(BOOT_ID);
  char *android_id = extract_string(j, "android_id");
  char *model = extract_string(j, "model");
  char *fingerprint = extract_string(j, "fingerprint");
  printf("{");
  printf("\"ok\":true,");
  printf("\"android_id\":"); print_json_string(android_id); printf(",");
  printf("\"boot_id\":"); print_json_string(boot); printf(",");
  printf("\"model\":"); print_json_string(model); printf(",");
  printf("\"fingerprint\":"); print_json_string(fingerprint); printf("}\n");
  free(j); free(boot); free(android_id); free(model); free(fingerprint);
  return 0;
}

static int bluetooth_address_valid(const char *value) {
  if (!value || strlen(value) != 17) return 0;
  for (int i=0;i<17;i++) {
    if (i%3==2) { if(value[i]!=':')return 0; }
    else if (!strchr("0123456789ABCDEF",value[i])) return 0;
  }
  return 1;
}

#ifdef XENOID_PROFILE_TESTING
#define BT_UID getuid()
#define BT_GID getgid()
#define BT_LABEL_ATTRIBUTE "user.xenoid.test.selinux"
#else
#define BT_UID 0
#define BT_GID 0
#define BT_LABEL_ATTRIBUTE "security.selinux"
#endif

static int bluetooth_open_directory(void) {
  char path[PATH_MAX];
  if (strlen(PROFILE_DIR)>=sizeof(path) || PROFILE_DIR[0]!='/') return -1;
  strcpy(path,PROFILE_DIR);
  int fd=open("/",O_RDONLY|O_DIRECTORY|O_CLOEXEC);
  if(fd<0)return -1;
  char *save=NULL;
  for(char *part=strtok_r(path,"/",&save);part;part=strtok_r(NULL,"/",&save)) {
    if(!strcmp(part,".")||!strcmp(part,"..")){close(fd);return -1;}
    int next=openat(fd,part,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
    close(fd);if(next<0)return -1;fd=next;
  }
  struct stat st;
  if(fstat(fd,&st)||st.st_uid!=BT_UID||st.st_gid!=BT_GID||((st.st_mode&07777)!=0700&&(st.st_mode&07777)!=0755)) {
    close(fd);return -1;
  }
  if(flock(fd,LOCK_EX)){close(fd);return -1;}
  return fd;
}

static int bluetooth_read_at(int directory,char output[19],struct stat *metadata) {
  int fd=openat(directory,"bluetooth_address",O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC);
  if(fd<0)return errno==ENOENT?0:-1;
  int ok=!fstat(fd,metadata)&&S_ISREG(metadata->st_mode)&&metadata->st_nlink==1&&
      metadata->st_uid==BT_UID&&metadata->st_gid==BT_GID&&(metadata->st_mode&0777)==0600&&metadata->st_size==18;
  ssize_t count=ok?read(fd,output,19):-1;close(fd);
  if(count!=18||output[17]!='\n')return -1;
  output[17]=0;return bluetooth_address_valid(output)?1:-1;
}

static int bluetooth_set_label(int fd) {
  const char label[]="u:object_r:shell_data_file:s0";
  char readback[sizeof(label)];ssize_t size;
#ifdef __APPLE__
  if(fsetxattr(fd,BT_LABEL_ATTRIBUTE,label,sizeof(label),0,0))return 0;
  size=fgetxattr(fd,BT_LABEL_ATTRIBUTE,readback,sizeof(readback),0,0);
#else
  if(fsetxattr(fd,BT_LABEL_ATTRIBUTE,label,sizeof(label),0))return 0;
  size=fgetxattr(fd,BT_LABEL_ATTRIBUTE,readback,sizeof(readback));
#endif
  return size==(ssize_t)sizeof(label)&&!memcmp(label,readback,sizeof(label));
}

static int set_bluetooth_address(const char *address) {
#ifndef XENOID_PROFILE_TESTING
  if(geteuid()!=0){fprintf(stderr,"bluetooth_identity:root_required\n");return 1;}
#endif
  if(!bluetooth_address_valid(address)){fprintf(stderr,"bluetooth_identity:invalid_address\n");return 1;}
  int directory=bluetooth_open_directory();
  if(directory<0){fprintf(stderr,"bluetooth_identity:unsafe_directory\n");return 1;}
  char previous[19], current[19], temporary[80]={0},line[19];
  struct stat before,now,created;
  int existed=bluetooth_read_at(directory,previous,&before),fd=-1,ok=0,published=0,created_valid=0;
  if(existed<0)goto done;
  snprintf(line,sizeof(line),"%s\n",address);
  for(int attempt=0;attempt<64;attempt++){
    snprintf(temporary,sizeof(temporary),".bluetooth-address.%ld.%d",(long)getpid(),attempt);
    fd=openat(directory,temporary,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
    if(fd>=0)break;
    if(errno!=EEXIST)goto done;
  }
  if(fd<0)goto done;
  if(fstat(fd,&created))goto done;
  created_valid=1;
  if(fchmod(fd,0600)||fchown(fd,BT_UID,BT_GID)||!bluetooth_set_label(fd))goto done;
  size_t done=0;
  while(done<18){ssize_t count=write(fd,line+done,18-done);if(count<0&&errno==EINTR)continue;if(count<=0)goto done;done+=(size_t)count;}
  if(fsync(fd))goto done;
  int observed=bluetooth_read_at(directory,current,&now);
  if(observed!=existed||(existed&&(now.st_dev!=before.st_dev||now.st_ino!=before.st_ino||strcmp(current,previous))))goto done;
  if(renameat(directory,temporary,directory,"bluetooth_address"))goto done;
  published=1;
  if(fsync(directory))goto done;
  if(bluetooth_read_at(directory,current,&now)!=1||strcmp(current,address)||now.st_dev!=created.st_dev||now.st_ino!=created.st_ino)goto done;
  ok=1;
done:
  if(fd>=0)close(fd);
  if(!published&&temporary[0]){
    struct stat pending;
    if(!fstatat(directory,temporary,&pending,AT_SYMLINK_NOFOLLOW)&&created_valid&&
       pending.st_dev==created.st_dev&&pending.st_ino==created.st_ino)unlinkat(directory,temporary,0);
  }
  close(directory);
  if(ok)printf("{\"ok\":true,\"durable\":true}\n");
  else fprintf(stderr,"bluetooth_identity:publication_failed\n");
  return ok?0:1;
}

int main(int argc, char **argv) {
  if (argc < 2 || !strcmp(argv[1], "status")) return status();
  if (!strcmp(argv[1], "dump")) return dump();
  if (!strcmp(argv[1], "env")) return env();
  if (!strcmp(argv[1], "get") && argc > 2) return get_key(argv[2]);
  if (!strcmp(argv[1], "set-bluetooth-address") && argc == 3) return set_bluetooth_address(argv[2]);
  fprintf(stderr, "usage: %s status | dump | env | get <key> | set-bluetooth-address <MAC>\n", argv[0]);
  return 64;
}
