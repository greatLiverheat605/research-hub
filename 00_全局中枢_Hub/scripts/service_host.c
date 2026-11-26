/* A stable per-app identity for the macOS Documents privacy prompt. */
#include <dirent.h>
#include <errno.h>
#include <limits.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>
extern char **environ;
int main(int argc, char **argv) {
    if (argc < 3 || !getenv("RH_EXCHANGE_ROOT")) return 64;
    char projects[PATH_MAX];
    snprintf(projects, sizeof projects, "%s/01_在研课题_Projects", getenv("RH_EXCHANGE_ROOT"));
    int logged = 0;
    for (;;) {
        DIR *d = opendir(projects);
        if (d) { closedir(d); break; }
        if (!logged++) perror("ResearchHub: waiting for access to project directory");
        sleep(10);
    }
    const char *ready = getenv("RH_AGENT_READY");
    if (ready) { FILE *f = fopen(ready,"w"); if (f) { fputs("ready\n",f); fclose(f); } }
    pid_t child;
    if (posix_spawn(&child, argv[1], NULL, NULL, argv+1, environ)) return 70;
    int status;
    while (waitpid(child, &status, 0) == -1) if (errno != EINTR) return 71;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
