// Transcriber.exe: startet den Transcriber. Gebaut mit dem C#-Compiler, der in Windows schon dabei ist
// (siehe build.py), gleiches Prinzip wie bei SkyWave:
// - Portabler Ordner: python\pythonw.exe und app\ liegen neben der exe -> direkt starten.
// - Einzelne Datei (Transcriber-<Version>.exe): Python, Pakete und App stecken als ZIP in der exe
//   (Ressource "Transcriber.payload"). Der erste Start einer Version entpackt sie einmal nach
//   %LOCALAPPDATA%\Transcriber\program\<Version> (mit Fortschrittsfenster) und räumt ältere Versionen
//   dort weg; spätere Starts gehen direkt los.
// Gestartet wird immer das signierte pythonw.exe von python.org. Ergebnisse, Modelle und Einstellungen
// liegen neben der exe (Umgebungsvariable TRANSCRIBER_DATA), nie im Programmordner.
// Hinweis: csc.exe aus .NET Framework 4 kann nur C# 5 (kein $"...", kein ?.).
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("Transcriber")]
[assembly: AssemblyProduct("Transcriber")]
[assembly: AssemblyDescription("Audio und Video lokal in Text umwandeln")]
[assembly: AssemblyVersion("0.3.0.0")]
[assembly: AssemblyFileVersion("0.3.0.0")]

static class TranscriberLauncher
{
    const string PayloadName = "Transcriber.payload";
    const string ReadyMarker = ".ready";
    static volatile bool unpackDone;

    [STAThread]
    static int Main(string[] args)
    {
        string exeDir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        string home = exeDir;
        if (!File.Exists(Path.Combine(home, "python", "pythonw.exe")))
        {
            Stream payload = Assembly.GetExecutingAssembly().GetManifestResourceStream(PayloadName);
            if (payload == null)
            {
                MessageBox.Show("Python fehlt (python\\pythonw.exe). Bitte den ganzen Ordner aus der ZIP-Datei entpacken.", "Transcriber");
                return 1;
            }
            using (payload)
            {
                try
                {
                    home = Unpacked(payload);
                }
                catch (Exception e)
                {
                    MessageBox.Show("Transcriber konnte nicht eingerichtet werden: " + e.Message, "Transcriber",
                        MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return 1;
                }
            }
        }
        return Launch(home, exeDir, args);
    }

    static string Quote(string s)
    {
        // Regeln von CommandLineToArgvW: Backslashes vor einem " verdoppeln
        var sb = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in s)
        {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') sb.Append('\\', slashes * 2 + 1);
            else sb.Append('\\', slashes);
            slashes = 0;
            sb.Append(c);
        }
        sb.Append('\\', slashes * 2);
        sb.Append('"');
        return sb.ToString();
    }

    static int Launch(string home, string exeDir, string[] args)
    {
        var arguments = new StringBuilder(Quote(Path.Combine(home, "app", "main.pyw")));
        foreach (string a in args) arguments.Append(' ').Append(Quote(a));   // z. B. auf die exe gezogene Dateien
        var start = new ProcessStartInfo
        {
            FileName = Path.Combine(home, "python", "pythonw.exe"),
            Arguments = arguments.ToString(),
            WorkingDirectory = home,
            UseShellExecute = false
        };
        start.EnvironmentVariables["TRANSCRIBER_DATA"] = exeDir;
        start.EnvironmentVariables["TRANSCRIBER_EXE"] = Application.ExecutablePath;
        start.EnvironmentVariables["PYTHONUTF8"] = "1";
        start.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
        start.EnvironmentVariables.Remove("PYTHONPATH");
        start.EnvironmentVariables.Remove("PYTHONHOME");
        try
        {
            Process.Start(start);
        }
        catch (Exception e)
        {
            MessageBox.Show("Transcriber konnte nicht starten: " + e.Message, "Transcriber");
            return 1;
        }
        return 0;
    }

    /// <summary>Der Ordner mit Python und App dieser exe; falls nötig zuerst entpackt.</summary>
    static string Unpacked(Stream payload)
    {
        string root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Transcriber", "program");
        // Version plus Grösse: eine neu gebaute exe derselben Version bekommt ihren eigenen Ordner.
        string name = Assembly.GetExecutingAssembly().GetName().Version.ToString(3) + "-" + payload.Length.ToString("x");
        string target = Path.Combine(root, name);
        using (var mutex = new Mutex(false, "Local\\TranscriberUnpack"))
        {
            bool ready = File.Exists(Path.Combine(target, ReadyMarker));
            bool owned;
            try
            {
                owned = mutex.WaitOne(ready ? 0 : Timeout.Infinite);
            }
            catch (AbandonedMutexException)
            {
                owned = true;
            }
            if (!owned) return target;
            try
            {
                if (!File.Exists(Path.Combine(target, ReadyMarker)))
                {
                    Directory.CreateDirectory(root);
                    string temp = target + ".part";
                    if (Directory.Exists(temp)) Directory.Delete(temp, true);
                    if (Directory.Exists(target)) Directory.Delete(target, true);
                    UnpackWithProgress(payload, temp);
                    File.WriteAllText(Path.Combine(temp, ReadyMarker), name);
                    Directory.Move(temp, target);
                }
                RemoveOld(root, name);
            }
            finally
            {
                mutex.ReleaseMutex();
            }
        }
        return target;
    }

    static void UnpackWithProgress(Stream payload, string dest)
    {
        Exception error = null;
        unpackDone = false;
        Application.EnableVisualStyles();
        var form = new Form
        {
            Text = "Transcriber",
            FormBorderStyle = FormBorderStyle.FixedDialog,
            MaximizeBox = false,
            MinimizeBox = false,
            StartPosition = FormStartPosition.CenterScreen,
            ClientSize = new Size(420, 92),
            TopMost = true
        };
        try
        {
            form.Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        }
        catch (Exception)
        {
        }
        var label = new Label
        {
            Text = "Transcriber wird eingerichtet (nur beim ersten Start einer Version) …",
            AutoSize = false,
            Location = new Point(16, 16),
            Size = new Size(388, 22)
        };
        var bar = new ProgressBar { Location = new Point(16, 48), Size = new Size(388, 22), Maximum = 1000 };
        form.Controls.Add(label);
        form.Controls.Add(bar);
        form.FormClosing += (s, e) => { if (!unpackDone) e.Cancel = true; };
        var worker = new Thread(() =>
        {
            try
            {
                Unpack(payload, dest, p => form.BeginInvoke((Action)(() => bar.Value = p)));
            }
            catch (Exception e)
            {
                error = e;
            }
            unpackDone = true;
            form.BeginInvoke((Action)(() => form.Close()));
        });
        worker.IsBackground = true;
        form.Shown += (s, e) => worker.Start();
        Application.Run(form);
        if (error != null) throw error;
    }

    static void Unpack(Stream payload, string dest, Action<int> progress)
    {
        using (var zip = new ZipArchive(payload, ZipArchiveMode.Read, true))
        {
            long total = 0;
            foreach (var entry in zip.Entries) total += entry.Length;
            long written = 0;
            int last = -1;
            string inside = Path.GetFullPath(dest).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            var buffer = new byte[1 << 16];
            foreach (var entry in zip.Entries)
            {
                string path = Path.GetFullPath(Path.Combine(dest, entry.FullName));
                if (!path.StartsWith(inside, StringComparison.OrdinalIgnoreCase)) continue;
                if (entry.FullName.EndsWith("/"))
                {
                    Directory.CreateDirectory(path);
                    continue;
                }
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                using (var input = entry.Open())
                using (var output = File.Create(path))
                {
                    int n;
                    while ((n = input.Read(buffer, 0, buffer.Length)) > 0)
                    {
                        output.Write(buffer, 0, n);
                        written += n;
                        int p = (int)(written * 1000 / Math.Max(1L, total));
                        if (p != last)
                        {
                            last = p;
                            progress(Math.Min(p, 1000));
                        }
                    }
                }
            }
        }
    }

    /// <summary>Entfernt ältere entpackte Versionen, ausser einer, die gerade läuft.</summary>
    static void RemoveOld(string root, string keep)
    {
        try
        {
            foreach (string dir in Directory.GetDirectories(root))
            {
                if (string.Equals(Path.GetFileName(dir), keep, StringComparison.OrdinalIgnoreCase)) continue;
                if (InUse(dir)) continue;
                try
                {
                    Directory.Delete(dir, true);
                }
                catch (Exception)
                {
                }
            }
        }
        catch (Exception)
        {
        }
    }

    static bool InUse(string dir)
    {
        string prefix = Path.GetFullPath(dir).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        foreach (string exe in new[] { "pythonw", "python" })
        {
            foreach (var p in Process.GetProcessesByName(exe))
            {
                try
                {
                    if (p.MainModule.FileName.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) return true;
                }
                catch (Exception)
                {
                }
                finally
                {
                    p.Dispose();
                }
            }
        }
        return false;
    }
}
