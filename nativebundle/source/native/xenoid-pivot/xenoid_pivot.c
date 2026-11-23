/* xenoid_pivot: static helper — make-rprivate /, loop-mount rootfs image, pivot_root, move essential mounts, detach old root, exec real init.
 * Usage: xenoid_pivot <image> [init-path] [init args...]
 * Test mode: if init-path == "--test", print mountinfo diagnostics instead of exec.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <linux/loop.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/syscall.h>
#include <sys/ioctl.h>
#include <sys/sysmacros.h>
#include <unistd.h>

#ifndef MS_REC
#define MS_REC 16384
#endif
#ifndef MNT_DETACH
#define MNT_DETACH 2
#endif

static void die(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fprintf(stderr, ": %s\n", strerror(errno));
    _exit(1);
}

static void xmkdir(const char *p) {
    if (mkdir(p, 0755) && errno != EEXIST) die("mkdir %s", p);
}

static void xmkdir_p(const char *p) {
    char buf[512];
    snprintf(buf, sizeof(buf), "%s", p);
    for (char *s = buf + 1; *s; s++) {
        if (*s == '/') { *s = 0; xmkdir(buf); *s = '/'; }
    }
    xmkdir(buf);
}

static void make_block_alias(const char *alias, int fd) {
    struct stat st;
    if (fstat(fd, &st) < 0) die("fstat block device");
    xmkdir_p("/dev/block");
    if (unlink(alias) < 0 && errno != ENOENT) die("unlink %s", alias);
    if (mknod(alias, S_IFBLK | 0600, st.st_rdev) < 0) die("mknod %s", alias);
}

static void make_userdata_alias(void) {
    const char *directory = "/dev/block/platform/14700000.ufs/by-name";
    const char *alias = "/dev/block/platform/14700000.ufs/by-name/userdata";
    xmkdir_p(directory);
    if (unlink(alias) < 0 && errno != ENOENT) die("unlink %s", alias);
    if (symlink("../../../sda", alias) < 0) die("symlink %s", alias);
}

/* Use the loop driver's direct-I/O path so image blocks do not occupy a
 * second, backing-file page cache inside the Android memory cgroup.  Configure
 * atomically and verify the kernel accepted DIO; unsupported hosts fail before
 * mounting rather than silently reverting to buffered I/O. */
static int loop_attach_rw(const char *img, char *loopdev, size_t n, int rw) {
    int ctl = open("/dev/loop-control", O_RDWR | O_CLOEXEC);
    if (ctl < 0) die("open loop-control");
    int idx = ioctl(ctl, LOOP_CTL_GET_FREE);
    close(ctl);
    if (idx < 0) die("LOOP_CTL_GET_FREE");
    snprintf(loopdev, n, "/dev/loop%d", idx);
    int imgfd = open(img, (rw ? O_RDWR : O_RDONLY) | O_CLOEXEC);
    if (imgfd < 0) die("open image %s", img);
    struct stat image_stat;
    if (fstat(imgfd, &image_stat) < 0) die("fstat image %s", img);
    int lfd = open(loopdev, (rw ? O_RDWR : O_RDONLY) | O_CLOEXEC);
    if (lfd < 0) {
        mknod(loopdev, S_IFBLK | 0600, makedev(7, idx));
        lfd = open(loopdev, (rw ? O_RDWR : O_RDONLY) | O_CLOEXEC);
    }
    if (lfd < 0) die("open %s", loopdev);
    struct loop_config config = {0};
    config.fd = imgfd;
    config.info.lo_flags = LO_FLAGS_AUTOCLEAR | LO_FLAGS_DIRECT_IO |
                           (rw ? 0 : LO_FLAGS_READ_ONLY);
    if (ioctl(lfd, LOOP_CONFIGURE, &config) < 0) {
        int saved = errno;
        /* Configuration never succeeded: a racing owner's loop is not ours. */
        close(lfd);
        close(imgfd);
        errno = saved;
        die("LOOP_CONFIGURE direct I/O %s", loopdev);
    }
    struct loop_info64 actual = {0};
    int status = ioctl(lfd, LOOP_GET_STATUS64, &actual);
    unsigned int required = config.info.lo_flags;
    if (status < 0 || (actual.lo_flags & required) != required ||
        (!!(actual.lo_flags & LO_FLAGS_READ_ONLY) != !rw) ||
        actual.lo_device != (unsigned long long)image_stat.st_dev ||
        actual.lo_inode != (unsigned long long)image_stat.st_ino) {
        int saved = status < 0 ? errno : EPROTO;
        /* Only a successful atomic configuration gives us detach ownership. */
        ioctl(lfd, LOOP_CLR_FD, 0);
        close(lfd);
        close(imgfd);
        errno = saved;
        die("LOOP_GET_STATUS64 direct I/O verification %s", loopdev);
    }
    close(imgfd);
    return lfd;
}
static int loop_attach(const char *img, char *loopdev, size_t n) {
    return loop_attach_rw(img, loopdev, n, 0);
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: %s <rootfs.img> <data.img|-> [--test|init args...]\n", argv[0]);
        return 2;
    }
    const char *image = argv[1];
    const char *data_img = strcmp(argv[2], "-") ? argv[2] : NULL;
    int next = 3;

    /* 1. kill shared propagation inherited from container runtime */
    if (mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL) < 0)
        die("make-rprivate /");

    /* 2. attach + mount the ext4 rootfs image read-only at /tmp/.newroot */
    xmkdir_p("/tmp/.newroot");
    char loopdev[64];
    int lfd = loop_attach(image, loopdev, sizeof(loopdev));
    const char *rootdev = "/dev/block/dm-0";
    make_block_alias(rootdev, lfd);
    if (mount(rootdev, "/tmp/.newroot", "ext4", MS_RDONLY, NULL) < 0)
        die("mount %s", rootdev);
    close(lfd);

    /* 3. pivot into it */
    xmkdir("/tmp/.newroot/oldroot");
    if (chdir("/tmp/.newroot")) die("chdir newroot");
    if (syscall(SYS_pivot_root, ".", "oldroot") < 0) die("pivot_root");

    /* 4. move container-provided mounts into the new tree (child mounts follow).
     *    /proc is deliberately NOT moved: Android init mounts a fresh single
     *    procfs, keeping mountinfo to exactly one /proc line (device-id checks
     *    compare /proc file st_dev against the mountinfo entry). */
    static const char *mv[][2] = {
        {"/oldroot/dev", "/dev"},
        {"/oldroot/sys", "/sys"},
        {NULL, NULL},
    };
    for (int i = 0; mv[i][0]; i++) {
        xmkdir(mv[i][1]);
        if (mount(mv[i][0], mv[i][1], NULL, MS_MOVE, NULL) < 0)
            fprintf(stderr, "warn: move %s -> %s: %s\n", mv[i][0], mv[i][1], strerror(errno));
    }

    /* 5. /data: real loop-mounted ext4 image when provided (no volume path leaks);
     *    otherwise fall back to moving the runtime volume mount. */
    xmkdir("/data");
    if (data_img) {
        char dloop[64];
        char dpath[512];
        snprintf(dpath, sizeof(dpath), "/oldroot%s", data_img);
        int dlfd = loop_attach_rw(dpath, dloop, sizeof(dloop), 1);
        const char *datadev = "/dev/block/sda";
        make_block_alias(datadev, dlfd);
        make_userdata_alias();
        if (mount(datadev, "/data", "ext4", MS_NOSUID | MS_NODEV | MS_NOATIME, NULL) < 0)
            die("mount data %s", datadev);
        close(dlfd);
    } else {
        if (mount("/oldroot/data", "/data", NULL, MS_MOVE, NULL) < 0)
            fprintf(stderr, "warn: move data: %s\n", strerror(errno));
        /* self-bind so mountinfo at least shows fsroot "/" */
        if (mount("/data", "/data", NULL, MS_BIND, NULL) < 0)
            fprintf(stderr, "warn: data self-bind: %s\n", strerror(errno));
    }

    /* 6. tmpfs staging for the proc overlay helper (keeps host paths out of mountinfo) */
    xmkdir("/xenoid");
    if (mount("tmpfs", "/xenoid", "tmpfs", 0, "mode=755") < 0)
        fprintf(stderr, "warn: tmpfs /xenoid: %s\n", strerror(errno));

    /* 7. detach the old overlay root entirely (volume mounts under it disappear) */
    umount2("/oldroot", MNT_DETACH);

    if (argc >= next && !strcmp(argv[next], "--test")) {
        /* diagnostics: dump mountinfo + statfs */
        char buf[4096];
        int fd = open("/proc/self/mountinfo", O_RDONLY);
        ssize_t r;
        if (fd >= 0) {
            while ((r = read(fd, buf, sizeof(buf))) > 0) write(1, buf, r);
            close(fd);
        }
        struct statfs sf;
        const char *paths[] = {"/", "/system", "/vendor", "/apex", "/data", NULL};
        for (int i = 0; paths[i]; i++) {
            if (statfs(paths[i], &sf) == 0)
                printf("STATFS %s 0x%lx\n", paths[i], (unsigned long)sf.f_type);
        }
        umount2("/", MNT_DETACH);
        _exit(0);
    }

    /* 8. exec the real init as PID 1 replacement */
    const char *init = argc > next ? argv[next] : "/init";
    execv(init, argv + next);
    die("execv %s", init);
    return 1;
}
