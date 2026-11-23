/* Pre-init metadata replay. Never creates/rewrites effective.json or ID leaves.
 * Existing userdata must first receive the selected profile via official Apply.
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/xattr.h>
#include <unistd.h>
#define MAX_JSON (256u*1024u)
#define MAX_TOKENS 16384
#define MAX_PLAN 128
struct token { int type, start, end, next, count; };
struct json { char *s; int length, pos, used; struct token t[MAX_TOKENS]; };
struct leaf { char name[96], value[512], temporary[96]; };
static const char *failure="invalid_profile";
static void space(struct json*j){while(j->pos<j->length&&strchr(" \t\r\n",j->s[j->pos]))j->pos++;}
static int hexchar(char c){return isxdigit((unsigned char)c);}
static int string_end(struct json*j){
 if(j->s[j->pos++]!='"')return 0;
 while(j->pos<j->length){unsigned char c=j->s[j->pos++];if(c=='"')return 1;if(c<32)return 0;
  if(c=='\\'){if(j->pos==j->length)return 0;c=j->s[j->pos++];if(c=='u'){for(int n=0;n<4;n++)if(j->pos==j->length||!hexchar(j->s[j->pos++]))return 0;}else if(!strchr("\"\\/bfnrt",c))return 0;}}
 return 0;
}
static int scalar(const struct json*j,int index,char*out,size_t cap){
 if(index<0||index>=j->used||cap==0)return 0;
 const struct token*t=&j->t[index];size_t n=0;
 if(t->type!='s'){
  if(t->type!='n'&&t->type!='b')return 0;
  size_t length=(size_t)(t->end-t->start);if(length>=cap)return 0;memcpy(out,j->s+t->start,length);out[length]=0;return 1;}
 for(int p=t->start+1;p<t->end-1;p++){
  unsigned char c=j->s[p];if(c=='\\'){c=j->s[++p];if(c=='u'){unsigned value=0;for(int k=0;k<4;k++){char x=j->s[++p];value=value*16+(unsigned)(isdigit((unsigned char)x)?x-'0':tolower((unsigned char)x)-'a'+10);}if(value<32||value>=127)return 0;c=(unsigned char)value;}else if(c=='n'||c=='r'||c=='b'||c=='f'||c=='t')return 0;}
  if(c<32||c>=127||n+1>=cap)return 0;out[n++]=(char)c;}
 out[n]=0;return 1;
}
static int member(const struct json*j,int object,const char*name){
 if(object<0||j->t[object].type!='o')return -1;
 for(int k=object+1;k<j->t[object].next;){char key[96];if(!scalar(j,k,key,sizeof(key)))return -1;int value=j->t[k].next;if(!strcmp(key,name))return value;k=j->t[value].next;}
 return -1;
}
static int parse_value(struct json*j,int depth){
 space(j);if(depth>48||j->pos>=j->length||j->used==MAX_TOKENS)return -1;
 int id=j->used++;struct token*t=&j->t[id];t->start=j->pos;char c=j->s[j->pos];
 if(c=='"'){t->type='s';if(!string_end(j))return -1;}
 else if(c=='{'||c=='['){t->type=c=='{'?'o':'a';j->pos++;space(j);char closing=c=='{'?'}':']';
  if(j->pos<j->length&&j->s[j->pos]!=closing)for(;;){
   if(t->type=='o'){if(j->s[j->pos]!='"')return -1;int key=parse_value(j,depth+1);char text[96];if(key<0||!scalar(j,key,text,sizeof(text)))return -1;
    for(int k=id+1;k<key;){char other[96];if(!scalar(j,k,other,sizeof(other))||!strcmp(text,other))return -1;k=j->t[j->t[k].next].next;}
    space(j);if(j->pos>=j->length||j->s[j->pos++]!=':')return -1;}
   if(parse_value(j,depth+1)<0)return -1;t->count++;space(j);if(j->pos>=j->length)return -1;
   if(j->s[j->pos]==closing)break;if(j->s[j->pos++]!=',')return -1;space(j);if(j->pos>=j->length||j->s[j->pos]==closing)return -1;}
  if(j->pos>=j->length||j->s[j->pos++]!=closing)return -1;}
 else if(c=='t'||c=='f'||c=='n'){const char*word=c=='t'?"true":c=='f'?"false":"null";size_t n=strlen(word);if((size_t)(j->length-j->pos)<n||memcmp(j->s+j->pos,word,n))return -1;j->pos+=(int)n;t->type='b';}
 else {t->type='n';if(c=='-')j->pos++;if(j->pos>=j->length)return -1;
  if(j->s[j->pos]=='0')j->pos++;else {if(j->s[j->pos]<'1'||j->s[j->pos]>'9')return -1;while(j->pos<j->length&&isdigit((unsigned char)j->s[j->pos]))j->pos++;}
  if(j->pos<j->length&&j->s[j->pos]=='.'){j->pos++;int start=j->pos;while(j->pos<j->length&&isdigit((unsigned char)j->s[j->pos]))j->pos++;if(start==j->pos)return -1;}
  if(j->pos<j->length&&(j->s[j->pos]=='e'||j->s[j->pos]=='E')){j->pos++;if(j->pos<j->length&&(j->s[j->pos]=='+'||j->s[j->pos]=='-'))j->pos++;int start=j->pos;while(j->pos<j->length&&isdigit((unsigned char)j->s[j->pos]))j->pos++;if(start==j->pos)return -1;}}
 t->end=j->pos;t->next=j->used;return id;
}
static int parse(struct json*j){j->pos=j->used=0;if(parse_value(j,0)!=0)return 0;space(j);return j->pos==j->length&&j->t[0].type=='o';}
static int lookup(const struct json*j,const char*path){
 int id=0;char text[160];if(strlen(path)>=sizeof(text))return -1;strcpy(text,path);char*save=NULL;
 for(char*part=strtok_r(text,".",&save);part;part=strtok_r(NULL,".",&save)){
  if(j->t[id].type=='o')id=member(j,id,part);
  else if(j->t[id].type=='a'){char*end;long index=strtol(part,&end,10);if(*end||index<0||index>=j->t[id].count)return -1;id++;while(index--)id=j->t[id].next;}
  else return -1;if(id<0)return -1;}
 return id;
}
static int equal(const struct json*a,int x,const struct json*b,int y){
 if(x<0||y<0||a->t[x].type!=b->t[y].type)return 0;
 int type=a->t[x].type;if(type=='o'){if(a->t[x].count!=b->t[y].count)return 0;for(int k=x+1;k<a->t[x].next;){char key[96];if(!scalar(a,k,key,sizeof(key)))return 0;int value=a->t[k].next;if(!equal(a,value,b,member(b,y,key)))return 0;k=a->t[value].next;}return 1;}
 if(type=='a'){if(a->t[x].count!=b->t[y].count)return 0;int p=x+1,q=y+1;while(p<a->t[x].next){if(!equal(a,p,b,q))return 0;p=a->t[p].next;q=b->t[q].next;}return 1;}
 char p[512],q[512];if(!scalar(a,x,p,sizeof(p))||!scalar(b,y,q,sizeof(q)))return 0;
 if(type=='n'){char*e1,*e2;errno=0;long double n1=strtold(p,&e1),n2=strtold(q,&e2);return !errno&&!*e1&&!*e2&&isfinite(n1)&&isfinite(n2)&&n1==n2;}return !strcmp(p,q);
}
static char*read_fd(int fd,size_t limit,size_t*length){
 struct stat s;if(fstat(fd,&s)||!S_ISREG(s.st_mode)||s.st_nlink!=1||s.st_size<=0||(size_t)s.st_size>limit)return NULL;
#ifndef COLD_SEED_TESTING
 if(s.st_uid!=0||(s.st_mode&0022))return NULL;
#endif
 char*data=calloc(1,(size_t)s.st_size+1);if(!data)return NULL;size_t done=0;while(done<(size_t)s.st_size){ssize_t n=read(fd,data+done,(size_t)s.st_size-done);if(n<=0){free(data);return NULL;}done+=(size_t)n;}*length=done;return data;
}
static int load_at(int directory,const char*name,struct json*j){int fd=openat(directory,name,O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC);if(fd<0)return 0;size_t n=0;j->s=read_fd(fd,MAX_JSON,&n);close(fd);j->length=(int)n;return j->s&&parse(j);}
static int nonunique(const char*name){return !strncmp(name,"cpu_",4)||!strncmp(name,"memory_",7)||!strncmp(name,"battery_",8)||!strncmp(name,"display_",8)||!strncmp(name,"input_",6)||!strncmp(name,"thermal_",8)||!strcmp(name,"usb_manufacturer")||!strcmp(name,"usb_product")||!strcmp(name,"usb_vendor_id")||!strcmp(name,"usb_product_id");}
static int add(struct leaf*plan,int*count,const struct json*j,const char*name,const char*path){
 if(*count==MAX_PLAN||!nonunique(name)||strlen(name)>=sizeof(plan[0].name))return 0;
 int token=lookup(j,path);if(token<0)return 0;
 int text=!strcmp(name,"cpu_implementer")||strstr(name,"_part")||strstr(name,"_model")||!strcmp(name,"battery_technology")||!strcmp(name,"input_name")||!strncmp(name,"usb_",4)||strstr(name,"_type");
 if(j->t[token].type!=(text?'s':'n'))return 0;
 struct leaf*p=&plan[(*count)++];strcpy(p->name,name);if(!scalar(j,token,p->value,sizeof(p->value)-1))return 0;
 if(!text){char*end;errno=0;(void)strtoll(p->value,&end,10);if(errno||*end)return 0;}
 strcat(p->value,"\n");return 1;
}
static char pinned_boot_id[64];
static int valid_boot_id(const char*s){
 if(strlen(s)!=36||s[14]<'1'||s[14]>'5'||!strchr("89ab",s[19]))return 0;
 for(int i=0;i<36;i++){if(i==8||i==13||i==18||i==23){if(s[i]!='-')return 0;}else if(!strchr("0123456789abcdef",s[i]))return 0;}return 1;
}
static int verify_ids(int dir,const struct json*j){
 static const char*keys[]={"android_id","boot_id","serial","imei","imeisv"};
 for(size_t i=0;i<sizeof(keys)/sizeof(keys[0]);i++){char path[64],value[128];snprintf(path,sizeof(path),"ids.%s",keys[i]);if(!scalar(j,lookup(j,path),value,sizeof(value))||!*value||!strcmp(value,"REGENERATE"))return 0;
  int fd=openat(dir,keys[i],O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC);if(fd<0)return 0;size_t n=0;char*s=read_fd(fd,128,&n);close(fd);if(!s)return 0;if(memchr(s,0,n)){free(s);return 0;}while(n&&(s[n-1]=='\n'||s[n-1]=='\r'))s[--n]=0;int ok;
  if(!strcmp(keys[i],"boot_id")){
   /* Official seed-boot-identity.sh rotates this sidecar before container
    * create without rewriting effective.json. Preserve that authoritative ID. */
   ok=valid_boot_id(value)&&valid_boot_id(s)&&(!*pinned_boot_id||!strcmp(pinned_boot_id,s));
   if(ok&&!*pinned_boot_id)snprintf(pinned_boot_id,sizeof(pinned_boot_id),"%s",s);
  }else ok=!strcmp(value,s);
  free(s);if(!ok)return 0;}
 return 1;
}
static int plan_leaves(struct leaf*p,int*count,const struct json*j){
 if(!add(p,count,j,"cpu_implementer","cpu.implementer"))return 0;
 snprintf(p[*count].name,sizeof(p[*count].name),"cpu_count");snprintf(p[(*count)++].value,sizeof(p[0].value),"8\n");
 const char*cpu[]={"processor","part","model","minimumFrequencyKhz","maximumFrequencyKhz"};
 for(int i=0;i<8;i++)for(size_t k=0;k<5;k++){char name[96],path[96];snprintf(name,sizeof(name),"cpu_%d_%s",i,cpu[k]);snprintf(path,sizeof(path),"cpu.cores.%d.%s",i,cpu[k]);if(!add(p,count,j,name,path))return 0;}
 const char*memory[]={"totalBytes","totalKiB","swapBytes"};for(size_t k=0;k<3;k++){char name[96],path[96];snprintf(name,sizeof(name),"memory_%s",memory[k]);snprintf(path,sizeof(path),"memory.%s",memory[k]);if(!add(p,count,j,name,path))return 0;}
 const char*battery[]={"capacityMah","minimumCapacityMah","chargeFullDesignUah","chargeFullUah","chargeCounterUah","health","level","plugged","present","scale","status","technology","temperature","voltage"};
 for(size_t k=0;k<14;k++){char name[96],path[96];snprintf(name,sizeof(name),"battery_%s",battery[k]);snprintf(path,sizeof(path),"battery.%s",battery[k]);if(!add(p,count,j,name,path))return 0;}
 const char*display[]={"width","height","densityDpi","physicalPpi","defaultRefreshRateHz","peakRefreshRateHz"};
 for(size_t k=0;k<6;k++){char name[96],path[96];snprintf(name,sizeof(name),"display_%s",k==2?"density_dpi":display[k]);snprintf(path,sizeof(path),"display.%s",display[k]);if(!add(p,count,j,name,path))return 0;}
 const char*input[]={"name","busType","vendorId","productId","version"};for(size_t k=0;k<5;k++){char name[96],path[96];snprintf(name,sizeof(name),"input_%s",input[k]);snprintf(path,sizeof(path),"input.%s",input[k]);if(!add(p,count,j,name,path))return 0;}
 for(int i=0;i<2;i++)if(!add(p,count,j,i?"input_height":"input_width",i?"display.height":"display.width"))return 0;
 const char*axis[]={"x","y","pressure","trackingId"};for(size_t k=0;k<4;k++)for(int n=0;n<2;n++){char name[96],path[96];snprintf(name,sizeof(name),"input_%s_%s",axis[k],n?"maximum":"minimum");snprintf(path,sizeof(path),"input.%s.%s",axis[k],n?"maximum":"minimum");if(!add(p,count,j,name,path))return 0;}
 const char*usb[]={"manufacturer","product","vendor_id","product_id"};for(size_t k=0;k<4;k++){char name[96],path[96];snprintf(name,sizeof(name),"usb_%s",usb[k]);snprintf(path,sizeof(path),"usb.%s",usb[k]);if(!add(p,count,j,name,path))return 0;}
 for(int i=0;i<5;i++)for(int n=0;n<2;n++){char name[96],path[96];snprintf(name,sizeof(name),"thermal_zone%d_%s",i,n?"type":"temp");snprintf(path,sizeof(path),"thermal.zone%d.%s",i,n?"type":"temp");if(!add(p,count,j,name,path))return 0;}
 return 1;
}
static int safe_target(int dir,const char*name){struct stat s;if(fstatat(dir,name,&s,AT_SYMLINK_NOFOLLOW))return errno==ENOENT;
 return S_ISREG(s.st_mode)&&s.st_nlink==1
#ifndef COLD_SEED_TESTING
 &&s.st_uid==0&&!(s.st_mode&0022)
#endif
 ;}
#ifdef COLD_SEED_TESTING
#define LABEL_ATTRIBUTE "user.xenoid.test.selinux"
#else
#define LABEL_ATTRIBUTE "security.selinux"
#endif
static const char shell_label[]="u:object_r:shell_data_file:s0";
static int label_matches(int fd){char value[128];ssize_t length;
#ifdef __APPLE__
 length=fgetxattr(fd,LABEL_ATTRIBUTE,value,sizeof(value),0,0);
#else
 length=fgetxattr(fd,LABEL_ATTRIBUTE,value,sizeof(value));
#endif
 return length==(ssize_t)sizeof(shell_label)&&!memcmp(value,shell_label,sizeof(shell_label));
}
static int set_label(int fd){int result;
#ifdef __APPLE__
 result=fsetxattr(fd,LABEL_ATTRIBUTE,shell_label,sizeof(shell_label),0,0);
#else
 result=fsetxattr(fd,LABEL_ATTRIBUTE,shell_label,sizeof(shell_label),0);
#endif
 return result==0&&label_matches(fd);
}
static int write_all(int fd,const char*s){size_t length=strlen(s);while(length){ssize_t n=write(fd,s,length);if(n<=0)return 0;s+=n;length-=(size_t)n;}return 1;}
static int open_directory(const char*path){
 if(!path||path[0]!='/'||strlen(path)>=PATH_MAX){errno=EINVAL;return -1;}
 char text[PATH_MAX];strcpy(text,path);int fd=open("/",O_RDONLY|O_DIRECTORY|O_CLOEXEC);if(fd<0)return -1;char*save=NULL;
 for(char*part=strtok_r(text,"/",&save);part;part=strtok_r(NULL,"/",&save)){
  if(!strcmp(part,".")||!strcmp(part,"..")){close(fd);errno=EINVAL;return -1;}
  int next=openat(fd,part,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);close(fd);if(next<0)return -1;fd=next;}
 return fd;
}
static int open_parent_directory(const char*directory){
 char parent[PATH_MAX];if(strlen(directory)>=sizeof(parent))return -1;strcpy(parent,directory);char*slash=strrchr(parent,'/');if(!slash)return -1;*slash=0;
 return open_directory(*parent?parent:"/");
}
static int publish_display(const char*directory,const struct json*j){
 int dir=open_parent_directory(directory);if(dir<0)return 0;
 struct leaf values[2]={0};strcpy(values[0].name,"xenoid-qa-display-mode");strcpy(values[1].name,"xenoid-qa-refresh-rate");
 char width[32],height[32],ppi[32],hz[32];int ok=0;
 if(!label_matches(dir))goto done;
 if(!scalar(j,lookup(j,"display.width"),width,sizeof(width))||!scalar(j,lookup(j,"display.height"),height,sizeof(height))||!scalar(j,lookup(j,"display.physicalPpi"),ppi,sizeof(ppi))||!scalar(j,lookup(j,"display.defaultRefreshRateHz"),hz,sizeof(hz)))goto done;
 snprintf(values[0].value,sizeof(values[0].value),"%s %s %s\n",width,height,ppi);snprintf(values[1].value,sizeof(values[1].value),"%s\n",hz);
 for(int i=0;i<2;i++)if(!safe_target(dir,values[i].name))goto done;
 for(int i=0;i<2;i++){snprintf(values[i].temporary,sizeof(values[i].temporary),".cold-display-%ld-%d",(long)getpid(),i);int fd=openat(dir,values[i].temporary,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);if(fd<0){values[i].temporary[0]=0;goto done;}int wrote=write_all(fd,values[i].value)&&fchmod(fd,0644)==0&&set_label(fd)&&fsync(fd)==0;close(fd);if(!wrote)goto done;}
 for(int i=0;i<2;i++){if(renameat(dir,values[i].temporary,dir,values[i].name))goto done;values[i].temporary[0]=0;}ok=fsync(dir)==0;
 done:for(int i=0;i<2;i++)if(values[i].temporary[0])unlinkat(dir,values[i].temporary,0);close(dir);return ok;
}
static int seed(const char*template_path,const char*directory){
 pinned_boot_id[0]=0;
 int dir=-1,tf=-1,ok=0,count=0;struct json *expected=calloc(1,sizeof(*expected)),*stored=calloc(1,sizeof(*stored));struct leaf*plan=calloc(MAX_PLAN,sizeof(*plan));if(!expected||!stored||!plan)goto end;
 dir=open_directory(directory);if(dir<0){failure="official_bootstrap_required";goto end;}
 int parent=open_parent_directory(directory);int labeled=parent>=0&&label_matches(parent)&&label_matches(dir);if(parent>=0)close(parent);
 if(!labeled){failure="profile_directory_label_mismatch";goto end;}
#ifndef COLD_SEED_TESTING
 struct stat ds;if(fstat(dir,&ds)||ds.st_uid!=0||(ds.st_mode&0022)){failure="unsafe_profile_directory";goto end;}
#endif
 tf=open(template_path,O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC);if(tf<0)goto end;size_t n=0;expected->s=read_fd(tf,MAX_JSON,&n);expected->length=(int)n;if(!expected->s||!parse(expected))goto end;
 if(!load_at(dir,"effective.json",stored)){failure="official_profile_apply_required";goto end;}
 static const char*groups[]={"schema","template","build","cpu","memory","display","input","storage","sensors","cameras"};for(size_t k=0;k<10;k++)if(!equal(expected,lookup(expected,groups[k]),stored,lookup(stored,groups[k]))){failure="stored_profile_metadata_mismatch";goto end;}
 const char*battery[]={"capacityMah","minimumCapacityMah","chargeFullDesignUah","technology","scale"};for(size_t k=0;k<5;k++){char path[96];snprintf(path,sizeof(path),"battery.%s",battery[k]);if(!equal(expected,lookup(expected,path),stored,lookup(stored,path))){failure="stored_battery_metadata_mismatch";goto end;}}
 const char*usb[]={"manufacturer","product","vendor_id","product_id"};for(size_t k=0;k<4;k++){char path[96];snprintf(path,sizeof(path),"usb.%s",usb[k]);if(!equal(expected,lookup(expected,path),stored,lookup(stored,path))){failure="stored_usb_metadata_mismatch";goto end;}}
 for(int i=0;i<5;i++){char path[96];snprintf(path,sizeof(path),"thermal.zone%d.type",i);if(!equal(expected,lookup(expected,path),stored,lookup(stored,path))){failure="stored_thermal_metadata_mismatch";goto end;}}
 if(!verify_ids(dir,stored)){failure="native_ids_missing_or_mismatched";goto end;}
 if(!plan_leaves(plan,&count,stored)){failure="sidecar_plan_invalid";goto end;}
 for(int i=0;i<count;i++)if(!safe_target(dir,plan[i].name)){failure="unsafe_sidecar_target";goto end;}
 for(int i=0;i<count;i++){snprintf(plan[i].temporary,sizeof(plan[i].temporary),".cold-seed-%ld-%d",(long)getpid(),i);int fd=openat(dir,plan[i].temporary,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);if(fd<0){plan[i].temporary[0]=0;failure="sidecar_stage_open_failed";goto end;}int wrote=write_all(fd,plan[i].value)&&fchmod(fd,0644)==0&&set_label(fd)&&fsync(fd)==0;close(fd);if(!wrote){failure="sidecar_stage_write_failed";goto end;}}
 /* No running Android process should exist yet. Still reject a concurrent
  * profile/identity change before publishing any replacement leaf. */
 struct json*again=calloc(1,sizeof(*again));if(!again)goto end;
 int stable=load_at(dir,"effective.json",again)&&again->length==stored->length&&!memcmp(again->s,stored->s,(size_t)stored->length)&&verify_ids(dir,stored);free(again->s);free(again);if(!stable){failure="profile_changed_during_seed";goto end;}
 for(int i=0;i<count;i++){if(renameat(dir,plan[i].temporary,dir,plan[i].name)){failure="sidecar_publish_failed";goto end;}plan[i].temporary[0]=0;}
 if(fsync(dir)){failure="sidecar_directory_sync_failed";goto end;}
 if(!publish_display(directory,stored)){failure="display_mode_publish_failed";goto end;}ok=1;
end:
 if(dir>=0&&plan)for(int i=0;i<count;i++)if(plan[i].temporary[0])unlinkat(dir,plan[i].temporary,0);
 if(tf>=0)close(tf);if(dir>=0)close(dir);if(expected){free(expected->s);free(expected);}if(stored){free(stored->s);free(stored);}free(plan);
 if(ok)printf("{\"ok\":true,\"sidecars\":%d,\"unique_ids_written\":false,\"effective_json_written\":false}\n",count);
 else fprintf(stderr,"xenoid-cold-seed:%s\n",failure);return ok?0:1;
}
int main(int argc,char**argv){
#ifndef COLD_SEED_TESTING
 if(geteuid()!=0){fprintf(stderr,"xenoid-cold-seed:root_required\n");return 1;}
#endif
 if(argc>=2&&!strcmp(argv[1],"--init")){
  if(seed("/system/etc/xenoid-cold-profile.json","/data/local/tmp/xenoid-profile"))return 1;
  argv[1]="/init";execv("/init",argv+1);perror("xenoid-cold-seed:exec_init");return 1;
 }
 if(argc!=3){fprintf(stderr,"xenoid-cold-seed:arguments\n");return 2;}return seed(argv[1],argv[2]);
}
