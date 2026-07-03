/* ioctl_stub — shim LD_PRELOAD per sbloccare i firmware che parlano con hardware assente
 * su QEMU (switch/MII/GPIO e memoria condivisa dei SoC MediaTek/Ralink). Senza, i demoni
 * di rete (es. `cos` su TP-Link MT7628) restano in loop d'init e non avviano i servizi.
 *
 * Copre due classi di fallimento, entrambe con la stessa strategia "chiama sempre il vero,
 * fingi successo solo se manca l'hardware":
 *
 *  1) ioctl() su registri/hardware non emulato → l'ioctl reale fallisce con
 *     EOPNOTSUPP/ENOTTY/ENODEV/ENXIO: ritorniamo 0 (es. swReg dello switch MT7628).
 *
 *  2) open() di device SoC assenti (es. /dev/cmem, l'allocatore di memoria contigua da cui
 *     `cos` prende il "big shared buffer"): il device non esiste → open reale fallisce con
 *     ENOENT/ENODEV/ENXIO. Restituiamo un fd su un memfd anonimo dimensionato, così il
 *     successivo mmap() del demone riesce (offset 0, coerente con l'ioctl di alloc che qui
 *     ritorna 0). Il buffer non è quello vero del SoC, ma sblocca cmem_initSharedBuff.
 *
 * ponytail: euristica su errno + allowlist di device (FAKE_DEVS), non reversing dei comandi
 * per-driver. Ceiling: se il demone LEGGE valori di registro plausibili, o si aspetta che il
 * fastpath hw riempia il buffer, qui non basta — è la frontiera nota di FirmAE su MediaTek.
 * Per estendere: aggiungi nomi a FAKE_DEVS. Non intercettiamo openat() (aggiungilo se serve).
 *
 * Compilare per l'arch del target (vedi assets/sources/ioctl_stub/build.sh):
 *   mipsel-linux-gcc -shared -fPIC -O2 -o ioctl_stub.mipsel.so ioctl_stub.c
 *
 * NB: usa syscall(...) invece di dlsym(RTLD_NEXT): la funzione reale è la syscall stessa, e
 * così l'unica dipendenza è libc (dlsym vive in libdl sulle uClibc vecchie del guest → il
 * preload non si caricherebbe).
 */
#define _GNU_SOURCE
#include <stdarg.h>
#include <errno.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/syscall.h>

/* device SoC assenti da rimpiazzare con un buffer di memoria (match per sottostringa). */
static const char *FAKE_DEVS[] = { "cmem", 0 };
#define FAKE_SIZE (16 * 1024 * 1024)   /* buffer condiviso finto: 16 MiB */

static int is_fake_dev(const char *path)
{
    if (!path)
        return 0;
    for (const char **d = FAKE_DEVS; *d; d++)
        if (strstr(path, *d))
            return 1;
    return 0;
}

static int make_backing(void)
{
    /* memfd anonimo dimensionato: mmap-abile, nessun file lasciato sul filesystem. */
    long fd = syscall(SYS_memfd_create, "sae_shim", 0);
    if (fd < 0)
        return -1;
    if (syscall(SYS_ftruncate, (int)fd, (long)FAKE_SIZE) < 0) {
        syscall(SYS_close, (int)fd);
        return -1;
    }
    return (int)fd;
}

int open(const char *path, int flags, ...)
{
    va_list ap;
    int mode;
    va_start(ap, flags);
    mode = va_arg(ap, int);
    va_end(ap);

    int fd = (int)syscall(SYS_open, path, flags, mode);
    if (fd < 0 && is_fake_dev(path)) {
        int b = make_backing();
        if (b >= 0)
            return b;
    }
    return fd;
}

int open64(const char *path, int flags, ...)
{
    va_list ap;
    int mode;
    va_start(ap, flags);
    mode = va_arg(ap, int);
    va_end(ap);
    return open(path, flags | O_LARGEFILE, mode);
}

int ioctl(int fd, unsigned long req, ...)
{
    va_list ap;
    void *arg;
    va_start(ap, req);
    arg = va_arg(ap, void *);
    va_end(ap);

    int ret = (int)syscall(SYS_ioctl, fd, req, arg);
    if (ret < 0 && (errno == EOPNOTSUPP || errno == ENOTTY ||
                    errno == ENODEV    || errno == ENXIO)) {
        /* hardware non emulato: fingi successo così il demone prosegue */
        return 0;
    }
    return ret;
}
