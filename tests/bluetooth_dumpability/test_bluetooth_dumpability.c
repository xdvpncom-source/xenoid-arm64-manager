#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

/* Only the pre-existing name-filter dependencies are inert in this fixture.
 * The complete production policy and prctl entry point are compiled verbatim
 * from xenoid_shim.c; UID, PID, /proc, prctl and signal masks are real kernel I/O. */
static int xenoid_reader_is_app_uid(void) { return 0; }
static int line_hidden(const char *name) { (void)name; return 0; }
#include "prctl-policy-under-test.inc"

#define CHECK(c) do { if (!(c)) { \
  fprintf(stderr,"FAIL %s:%d %s errno=%d\n",__FILE__,__LINE__,#c,errno); \
  return 1; } } while (0)

static int real_dumpable(void) {
  return (int)syscall(SYS_prctl,PR_GET_DUMPABLE,0UL,0UL,0UL,0UL);
}
static int wrapped_get(void) { return prctl(PR_GET_DUMPABLE,0UL,0UL,0UL,0UL); }
static int wrapped_set(unsigned long value) { return prctl(PR_SET_DUMPABLE,value,0UL,0UL,0UL); }
static int specialize(uid_t uid,int saved_root) {
  CHECK(setgroups(0,NULL)==0);
  CHECK(setresgid(uid,uid,saved_root?0:uid)==0);
  CHECK(setresuid(uid,uid,saved_root?0:uid)==0);
  CHECK(getuid()==uid && geteuid()==uid);
  CHECK(real_dumpable()==2); /* Test hosts must reproduce the measured policy. */
  return 0;
}
static uint64_t real_signal_mask(void) {
  uint64_t value=0;
  if (syscall(SYS_rt_sigprocmask,SIG_BLOCK,NULL,&value,sizeof(value))!=0) abort();
  return value;
}
static int debuggerd_restore(unsigned long original) {
  CHECK(wrapped_set(1)==0 && real_dumpable()==1);
  CHECK(wrapped_get()==1); /* Initialization must not undo debuggerd's SET1. */
  CHECK(wrapped_set(original)==0 && real_dumpable()==(int)original);
  errno=0;
  CHECK(wrapped_set(2)==-1 && errno==EINVAL);
  CHECK(real_dumpable()==(int)original); /* No fabricated return or flag. */
  return 0;
}
static int target(void) {
  CHECK(getuid()==0 && real_dumpable()==1);
  CHECK(wrapped_get()==1 && real_dumpable()==1); /* Zygote/UID0 is untouched. */
  CHECK(specialize(1002,0)==0);
  sigset_t blocked;sigemptyset(&blocked);sigaddset(&blocked,SIGUSR1);
  CHECK(pthread_sigmask(SIG_BLOCK,&blocked,NULL)==0);
  uint64_t before=real_signal_mask();errno=E2BIG;
  CHECK(wrapped_get()==0 && real_dumpable()==0 && errno==E2BIG);
  CHECK(real_signal_mask()==before);
  CHECK(debuggerd_restore(0)==0);
  return 0;
}
static int initial_name(void) {
  CHECK(specialize(1002,0)==0);
  CHECK(prctl(PR_SET_NAME,(unsigned long)"bt-policy-test",0UL,0UL,0UL)==0);
  CHECK(real_dumpable()==0);
  char name[16]={0};CHECK(prctl(PR_GET_NAME,(unsigned long)name,0UL,0UL,0UL)==0);
  CHECK(strcmp(name,"bt-policy-test")==0);
  return 0;
}
static int ignored(uid_t uid) {
  CHECK(specialize(uid,0)==0);
  CHECK(wrapped_get()==2 && real_dumpable()==2);
  return 0;
}
static int after_name(char *name) {
  CHECK(strlen(name)>=strlen("com.android.bluetooth"));
  CHECK(specialize(1002,0)==0);
  CHECK(wrapped_get()==2 && real_dumpable()==2); /* Not specialized by package yet. */
  memset(name,0,strlen(name));memcpy(name,"com.android.bluetooth",strlen("com.android.bluetooth"));
  CHECK(wrapped_get()==0 && real_dumpable()==0);
  return 0;
}
static int forked(void) {
  CHECK(specialize(1002,1)==0 && wrapped_get()==0 && real_dumpable()==0);
  pid_t child=fork();CHECK(child>=0);
  if(child==0) {
    CHECK(setresuid(1002,0,0)==0 && geteuid()==0);
    CHECK(real_dumpable()==2 && wrapped_get()==2); /* No UID0 normalization. */
    CHECK(setresuid(1002,1002,0)==0 && real_dumpable()==2);
    CHECK(wrapped_get()==0 && real_dumpable()==0); /* Parent's cached PID is not reused. */
    CHECK(debuggerd_restore(0)==0);_exit(0);
  }
  int status=0;CHECK(waitpid(child,&status,0)==child && WIFEXITED(status) && WEXITSTATUS(status)==0);
  CHECK(real_dumpable()==0 && wrapped_get()==0);
  return 0;
}
static pthread_barrier_t barrier;
static void *first_call(void *unused) {
  (void)unused;pthread_barrier_wait(&barrier);
  int ok=getuid()==1002 && geteuid()==1002 && wrapped_get()==0 && real_dumpable()==0;
  return (void *)(uintptr_t)!ok;
}
static int concurrent(void) {
  enum { COUNT=16 };pthread_t threads[COUNT];CHECK(pthread_barrier_init(&barrier,NULL,COUNT+1)==0);
  for(int i=0;i<COUNT;i++)CHECK(pthread_create(&threads[i],NULL,first_call,NULL)==0);
  CHECK(specialize(1002,0)==0);pthread_barrier_wait(&barrier);
  for(int i=0;i<COUNT;i++){void *value=NULL;CHECK(pthread_join(threads[i],&value)==0 && value==NULL);}
  CHECK(real_dumpable()==0);CHECK(debuggerd_restore(0)==0);
  return 0;
}
static int old_kernel_failure(void) {
  CHECK(specialize(1002,0)==0);
  int original=real_dumpable();CHECK(original==2);
  CHECK(syscall(SYS_prctl,PR_SET_DUMPABLE,1UL,0UL,0UL,0UL)==0);
  errno=0;CHECK(syscall(SYS_prctl,PR_SET_DUMPABLE,(unsigned long)original,0UL,0UL,0UL)==-1 && errno==EINVAL);
  CHECK(real_dumpable()==1);
  return 0;
}
int main(int argc,char **argv) {
  alarm(12);
  CHECK(argc==2 && getuid()==0 && geteuid()==0);
  CHECK(syscall(SYS_prctl,PR_SET_DUMPABLE,1UL,0UL,0UL,0UL)==0);
  int result;
  if(!strcmp(argv[1],"target"))result=target();
  else if(!strcmp(argv[1],"first-name"))result=initial_name();
  else if(!strcmp(argv[1],"uid1000"))result=ignored(1000);
  else if(!strcmp(argv[1],"uid10000"))result=ignored(10000);
  else if(!strcmp(argv[1],"wrong-name"))result=ignored(1002);
  else if(!strcmp(argv[1],"after-name"))result=after_name(argv[0]);
  else if(!strcmp(argv[1],"fork"))result=forked();
  else if(!strcmp(argv[1],"threads"))result=concurrent();
  else if(!strcmp(argv[1],"old-restore2"))result=old_kernel_failure();
  else {fprintf(stderr,"unknown case\n");return 1;}
  if(result==0)printf("PASS %s\n",argv[1]);
  return result;
}
