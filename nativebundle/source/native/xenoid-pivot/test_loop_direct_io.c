#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include <linux/loop.h>
static int mode, want_rw;
static unsigned int configured_flags;
static int fake_open(const char *path, int flags, ...) {
    if (!strcmp(path,"/dev/loop-control")) return 10;
    if (!strcmp(path,"fixture.img")) {
        if ((flags & O_ACCMODE) != (want_rw ? O_RDWR : O_RDONLY)) abort();
        return 11;
    }
    if (!strcmp(path,"/dev/loop7")) return 12;
    abort();
}
static int fake_close(int fd) { return fd >= 10 && fd <= 12 ? 0 : -1; }
static int fake_fstat(int fd, struct stat *s) {
    if (fd != 11) abort();
    memset(s,0,sizeof(*s)); s->st_dev=2049; s->st_ino=987654;
    s->st_mode=S_IFREG|0600; s->st_size=33554432; return 0;
}
static int fake_ioctl(int fd, unsigned long req, ...) {
    if (req==LOOP_CTL_GET_FREE) { if (fd!=10) abort(); return 7; }
    if (fd!=12) abort();
    va_list ap;va_start(ap,req);
    if (req==LOOP_CONFIGURE) {
        struct loop_config *c=va_arg(ap,struct loop_config *);
        if(c->fd!=11 || c->block_size!=0) abort();
        unsigned int flags=LO_FLAGS_AUTOCLEAR|LO_FLAGS_DIRECT_IO|(want_rw?0:LO_FLAGS_READ_ONLY);
        if(c->info.lo_flags!=flags) abort();
        configured_flags=flags;
        fprintf(stderr,"CONFIGURED flags=%u\n",flags);
        va_end(ap);
        if(mode==2) {errno=EOPNOTSUPP;return -1;}
        return 0;
    }
    if(req==LOOP_GET_STATUS64) {
        struct loop_info64 *v=va_arg(ap,struct loop_info64 *);
        v->lo_flags=configured_flags;v->lo_device=2049;v->lo_inode=987654;
        if(mode==3) v->lo_flags&=~LO_FLAGS_DIRECT_IO;
        if(mode==4) v->lo_flags^=LO_FLAGS_READ_ONLY;
        if(mode==5) v->lo_inode++;
        va_end(ap);
        if(mode==6) {errno=EIO;return -1;}
        return 0;
    }
    va_end(ap);
    if(req==LOOP_CLR_FD) {fprintf(stderr,"DETACH_OWNED\n");return 0;}
    abort();
}
#define open fake_open
#define close fake_close
#define fstat fake_fstat
#define ioctl fake_ioctl
#define main pivot_main
#include "xenoid_pivot.c"
#undef main
int main(int argc,char **argv) {
    if(argc!=2)return 2;mode=atoi(argv[1]);want_rw=mode==1;
    char path[64];int fd=loop_attach_rw("fixture.img",path,sizeof(path),want_rw);
    if(fd!=12 || strcmp(path,"/dev/loop7"))return 3;
    fprintf(stderr,"VERIFIED\n");return 0;
}
