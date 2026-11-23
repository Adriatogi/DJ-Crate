// The executable inside the downloadable DJ Crate.app. It runs the app's own Python:
//   DJ Crate.app/Contents/Resources/python/bin/python3 -m djcrate.main
// A compiled launcher (rather than a shell script) carries its code signature inside the file,
// so macOS doesn't call the app "damaged" when an unzip tool drops extended attributes.
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char *argv[]) {
    char exe[PATH_MAX], real[PATH_MAX], app[PATH_MAX], py[PATH_MAX + 64];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0 || !realpath(exe, real)) {
        fprintf(stderr, "DJ Crate: can't find where the app is\n");
        return 1;
    }
    // real = <app>/Contents/MacOS/DJ Crate  ->  app = <app>
    strncpy(app, real, sizeof(app) - 1);
    app[sizeof(app) - 1] = 0;
    for (int i = 0; i < 3; i++) {
        char *slash = strrchr(app, '/');
        if (!slash) return 1;
        *slash = 0;
    }
    snprintf(py, sizeof(py), "%s/Contents/Resources/python/bin/python3", app);

    setenv("DJCRATE_BUNDLE", app, 1);
    setenv("PYTHONNOUSERSITE", "1", 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);
    unsetenv("PYTHONHOME");
    unsetenv("PYTHONPATH");
    unsetenv("PYTHONSTARTUP");

    char **args = calloc((size_t)argc + 3, sizeof(char *));
    if (!args) return 1;
    args[0] = py;
    args[1] = "-m";
    args[2] = "djcrate.main";
    for (int i = 1; i < argc; i++) args[i + 2] = argv[i];
    execv(py, args);
    perror("DJ Crate: couldn't start Python");
    return 1;
}
