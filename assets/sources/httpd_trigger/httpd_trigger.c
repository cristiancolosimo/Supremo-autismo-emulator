/* httpd_trigger — switch-BYPASS shim per firmware TP-Link (MediaTek MT7628).
 *
 * Problema (diagnosi in README §SoC MediaTek/Ralink): httpd è message-driven — dopo aver
 * letto HTTP_CFG_OBJ dalla shm (porta 80, enabled=1) resta in _newselect su /var/tmp/8 e
 * bind-a la :80 SOLO quando riceve dal `cos` il trigger RDP "ServiceCfg" (msgType 0x7ee).
 * Su QEMU malta `cos` non arriva mai a inviarlo: il suo http-init è gated dietro la
 * prontezza dello switch (raeth.ko / swReg / /proc/tplink) che malta non emula.
 *
 * Fingere i valori dei registri swReg (vero "shim dello switch") richiede la mappa registri
 * ESW del MT7628 — il soffitto noto di FirmAE. Questo bypassa il gate: impersoniamo `cos` e
 * mandiamo noi il datagramma ServiceCfg a /var/tmp/8, così l'httpd VERO bind-a la :80 e
 * serve il backend dinamico (non la GUI statica di fallback).
 *
 * Wire format (ricavato da strace di cos, vedi scratchpad/costrace.txt): datagramma
 * AF_UNIX SOCK_DGRAM di 520 byte = [msgType u32 LE @0][flag u32 @4][payload @8].
 * rsl_sendHttpdServiceCfg (libcmm.so @0x1d02c) usa msgType=0x7ee, invia al modulo 8,
 * payload = 50 byte di ServiceCfg.
 *
 * ponytail: costruttore in una .so caricata via LD_PRELOAD in un processo qualsiasi
 * (es. `busybox true`) — stesso schema del già-funzionante ioctl_stub.so, niente _start
 * né numeri di syscall. socket/connect/sendto risolti a load-time da libc.so.0 del guest.
 * Ceiling: il payload ServiceCfg (50 byte @8) qui è tutto-zero tranne i knob ovvi; se
 * l'httpd lo valida e rifiuta, va riempito con i campi veri (index/enable/port) — tunabili
 * sotto senza ricompilare la logica. Override runtime: HTTPD_TRIGGER_PATH, _MSGTYPE, _FLAG.
 */
#define _GNU_SOURCE
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/un.h>

#define MSG_LEN   520
#define OFF_TYPE  0
#define OFF_FLAG  4
#define OFF_DATA  8

static unsigned long env_ul(const char *k, unsigned long dflt)
{
    const char *v = getenv(k);
    if (!v || !*v)
        return dflt;
    return strtoul(v, 0, 0);
}

__attribute__((constructor))
static void fire(void)
{
    const char *path = getenv("HTTPD_TRIGGER_PATH");
    if (!path || !*path)
        path = "/var/tmp/8";                 /* httpd = modulo RDP 8 */

    unsigned char msg[MSG_LEN];
    memset(msg, 0, sizeof msg);
    unsigned int msgtype = (unsigned int)env_ul("HTTPD_TRIGGER_MSGTYPE", 0x7ee);
    unsigned int flag    = (unsigned int)env_ul("HTTPD_TRIGGER_FLAG", 0);
    /* little-endian, come sul wire (guest è mipsel) */
    msg[OFF_TYPE+0]=msgtype; msg[OFF_TYPE+1]=msgtype>>8; msg[OFF_TYPE+2]=msgtype>>16; msg[OFF_TYPE+3]=msgtype>>24;
    msg[OFF_FLAG+0]=flag;    msg[OFF_FLAG+1]=flag>>8;    msg[OFF_FLAG+2]=flag>>16;    msg[OFF_FLAG+3]=flag>>24;
    /* payload ServiceCfg (msg[OFF_DATA..OFF_DATA+50]) resta 0: knob di tuning se serve. */

    int fd = socket(AF_UNIX, SOCK_DGRAM, 0);
    if (fd < 0)
        return;
    struct sockaddr_un sa;
    memset(&sa, 0, sizeof sa);
    sa.sun_family = AF_UNIX;
    strncpy(sa.sun_path, path, sizeof sa.sun_path - 1);
    sendto(fd, msg, MSG_LEN, 0, (struct sockaddr *)&sa, sizeof(sa.sun_family) + strlen(sa.sun_path));
    close(fd);
}
