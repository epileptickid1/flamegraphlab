#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <dbghelp.h>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;

static std::uint64_t filetime_ticks(FILETIME t) {
    return (static_cast<std::uint64_t>(t.dwHighDateTime) << 32) | t.dwLowDateTime;
}
static std::uint64_t unix_us() {
    FILETIME t{}; GetSystemTimePreciseAsFileTime(&t);
    return (filetime_ticks(t) - 116444736000000000ULL) / 10;
}
static std::string json_quote(const std::string& s) {
    std::ostringstream o; o << '"';
    for (unsigned char c : s) {
        switch (c) {
        case '"': o << "\\\""; break; case '\\': o << "\\\\"; break;
        case '\b': o << "\\b"; break; case '\f': o << "\\f"; break;
        case '\n': o << "\\n"; break; case '\r': o << "\\r"; break; case '\t': o << "\\t"; break;
        default: if (c < 0x20) o << "\\u" << std::hex << std::setw(4) << std::setfill('0') << static_cast<unsigned>(c) << std::dec;
                 else o << static_cast<char>(c);
        }
    }
    o << '"'; return o.str();
}
static std::string utf8(const std::wstring& s) {
    if (s.empty()) return {};
    int n = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, s.data(), static_cast<int>(s.size()), nullptr, 0, nullptr, nullptr);
    if (n <= 0) throw std::runtime_error("Cannot encode option as UTF-8");
    std::string out(static_cast<std::size_t>(n), '\0');
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, s.data(), static_cast<int>(s.size()), out.data(), n, nullptr, nullptr))
        throw std::runtime_error("Cannot encode option as UTF-8");
    return out;
}
static std::wstring quote_arg(const std::wstring& arg) {
    std::wstring out = L"\""; unsigned slashes = 0;
    for (wchar_t ch : arg) {
        if (ch == L'\\') { ++slashes; continue; }
        if (ch == L'"') { out.append(slashes * 2 + 1, L'\\'); out += L'"'; slashes = 0; continue; }
        out.append(slashes, L'\\'); slashes = 0; out += ch;
    }
    out.append(slashes * 2, L'\\'); out += L'"'; return out;
}
struct Handle {
    HANDLE h = nullptr;
    explicit Handle(HANDLE value = nullptr) : h(value) {}
    ~Handle() { if (h && h != INVALID_HANDLE_VALUE) CloseHandle(h); }
    Handle(const Handle&) = delete; Handle& operator=(const Handle&) = delete;
    operator HANDLE() const { return h; }
};
struct ResumeOnExit {
    HANDLE thread; bool active = true;
    explicit ResumeOnExit(HANDLE value) : thread(value) {}
    ~ResumeOnExit() { if (active) ResumeThread(thread); }
    void resume() { if (active) { active = false; if (ResumeThread(thread) == static_cast<DWORD>(-1)) throw std::runtime_error("ResumeThread failed"); } }
};
struct Sample { std::uint64_t timestamp; std::vector<std::string> frames; };
struct Counts { std::uint64_t attempts=0, succeeded=0, failed=0, suspend_failures=0, context_failures=0, empty_stacks=0; };

static std::vector<std::string> walk_stack(HANDLE process, HANDLE thread, CONTEXT context) {
    STACKFRAME64 frame{};
    frame.AddrPC.Offset=context.Rip; frame.AddrPC.Mode=AddrModeFlat;
    frame.AddrStack.Offset=context.Rsp; frame.AddrStack.Mode=AddrModeFlat;
    frame.AddrFrame.Offset=context.Rbp; frame.AddrFrame.Mode=AddrModeFlat;
    std::vector<std::string> frames;
    DWORD64 previous_pc=0, previous_sp=0;
    for (unsigned depth=0; depth<128 && frame.AddrPC.Offset; ++depth) {
        if (!StackWalk64(IMAGE_FILE_MACHINE_AMD64,process,thread,&frame,&context,nullptr,
                         SymFunctionTableAccess64,SymGetModuleBase64,nullptr)) break;
        if (!frame.AddrPC.Offset) break;
        const DWORD64 pc=frame.AddrPC.Offset, sp=frame.AddrStack.Offset;
        if(depth && pc==previous_pc && sp==previous_sp) break;
        previous_pc=pc; previous_sp=sp;
        char storage[sizeof(SYMBOL_INFO)+MAX_SYM_NAME]{};
        auto* symbol=reinterpret_cast<SYMBOL_INFO*>(storage);
        symbol->SizeOfStruct=sizeof(SYMBOL_INFO); symbol->MaxNameLen=MAX_SYM_NAME;
        DWORD64 displacement=0; std::string name;
        if (SymFromAddr(process,pc,&displacement,symbol)) name.assign(symbol->Name,symbol->NameLen);
        else {
            IMAGEHLP_MODULE64 module{}; module.SizeOfStruct=sizeof(module);
            std::ostringstream fallback;
            if (SymGetModuleInfo64(process,pc,&module)) fallback<<module.ModuleName<<"+0x"<<std::hex<<(pc-module.BaseOfImage);
            else fallback<<"0x"<<std::hex<<pc;
            name=fallback.str();
        }
        frames.push_back(std::move(name));
    }
    return frames;
}
static void usage() { std::wcerr<<L"Usage: sampler.exe --output PREFIX [--interval-ms N] -- EXE [ARG ...]\n"; }

int wmain(int argc, wchar_t** argv) {
 try {
    std::wstring prefix; DWORD interval=2; int sep=-1;
    for(int i=1;i<argc;++i) {
        std::wstring a(argv[i]);
        if(a==L"--") { sep=i; break; }
        if(a==L"--help") { usage(); return 0; }
        if(a==L"--output" && i+1<argc) prefix=argv[++i];
        else if(a==L"--interval-ms" && i+1<argc) {
            auto value=std::stoul(argv[++i]);
            if(value<1 || value>1000) throw std::invalid_argument("interval-ms must be in [1, 1000]");
            interval=static_cast<DWORD>(value);
        } else throw std::invalid_argument("unknown or incomplete sampler option: "+utf8(a));
    }
    if(prefix.empty() || sep<0 || sep+1>=argc) { usage(); throw std::invalid_argument("output prefix and child command are required"); }
    for(int i=sep+2;i<argc-1;++i) { if((std::wstring(argv[i])==L"--mode" && std::wstring(argv[i+1])==L"parallel") || std::wstring(argv[i])==L"--mode=parallel") throw std::invalid_argument("sampler supports sequential mode only; parallel worker threads are not sampled"); }
    fs::path child_path(prefix+L".child.json"), samples_path(prefix+L".samples.json"), stderr_path(prefix+L".child.stderr.txt");
    SECURITY_ATTRIBUTES inheritable{}; inheritable.nLength=sizeof(inheritable); inheritable.bInheritHandle=TRUE;
    Handle child_out(CreateFileW(child_path.c_str(),GENERIC_WRITE,FILE_SHARE_READ,&inheritable,CREATE_ALWAYS,FILE_ATTRIBUTE_NORMAL,nullptr));
    if(child_out.h==INVALID_HANDLE_VALUE) throw std::runtime_error("Cannot create child stdout file");
    Handle child_err(CreateFileW(stderr_path.c_str(),GENERIC_WRITE,FILE_SHARE_READ,&inheritable,CREATE_ALWAYS,FILE_ATTRIBUTE_NORMAL,nullptr));
    if(child_err.h==INVALID_HANDLE_VALUE) throw std::runtime_error("Cannot create child stderr file");
    Handle null_in(CreateFileW(L"NUL",GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE,&inheritable,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr));
    if(null_in.h==INVALID_HANDLE_VALUE) throw std::runtime_error("Cannot open NUL for child stdin");
    Handle job(CreateJobObjectW(nullptr,nullptr)); if(!job.h) throw std::runtime_error("CreateJobObject failed");
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{}; limits.BasicLimitInformation.LimitFlags=JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if(!SetInformationJobObject(job,JobObjectExtendedLimitInformation,&limits,sizeof(limits))) throw std::runtime_error("Cannot configure child cleanup job");
    std::wstring command=quote_arg(argv[sep+1]);
    for(int i=sep+2;i<argc;++i) { command+=L' '; command+=quote_arg(argv[i]); }
    std::vector<wchar_t> mutable_command(command.begin(),command.end()); mutable_command.push_back(L'\0');
    STARTUPINFOW si{}; si.cb=sizeof(si); si.dwFlags=STARTF_USESTDHANDLES; si.hStdInput=null_in; si.hStdOutput=child_out; si.hStdError=child_err;
    PROCESS_INFORMATION pi{};
    if(!CreateProcessW(argv[sep+1],mutable_command.data(),nullptr,nullptr,TRUE,CREATE_SUSPENDED|CREATE_NO_WINDOW,nullptr,nullptr,&si,&pi))
        throw std::runtime_error("CreateProcess failed for child executable");
    Handle process(pi.hProcess), thread(pi.hThread);
    if(!AssignProcessToJobObject(job,process)) { TerminateProcess(process,127); WaitForSingleObject(process,INFINITE); throw std::runtime_error("Cannot assign child to cleanup job"); }
    SymSetOptions(SymGetOptions()|SYMOPT_UNDNAME|SYMOPT_DEFERRED_LOADS|SYMOPT_FAIL_CRITICAL_ERRORS|SYMOPT_NO_PROMPTS|SYMOPT_DISABLE_SYMSRV_AUTODETECT);
    ResumeOnExit initial_resume(thread); initial_resume.resume();
    Sleep(10); 
    fs::path exe_dir=fs::absolute(argv[sep+1]).parent_path();
    if(exe_dir.empty()) exe_dir=fs::current_path();
    const std::wstring symbol_dir=exe_dir.wstring();
    if(!SymInitializeW(process,symbol_dir.c_str(),TRUE)) { TerminateProcess(process,127); WaitForSingleObject(process,INFINITE); throw std::runtime_error("SymInitialize failed"); }

    Counts counts{}; std::vector<Sample> samples; const auto observed_start=Clock::now(); std::uint64_t refresh_at=0;
    for(;;) {
        DWORD wait=WaitForSingleObject(process,interval);
        if(wait==WAIT_OBJECT_0) break;
        if(wait==WAIT_FAILED) throw std::runtime_error("WaitForSingleObject failed");
        ++counts.attempts;
        if(SuspendThread(thread)==static_cast<DWORD>(-1)) { ++counts.failed; ++counts.suspend_failures; continue; }
        const auto stamp=unix_us(); CONTEXT context{}; context.ContextFlags=CONTEXT_FULL; std::vector<std::string> frames;
        bool got_context=false;
        {
            ResumeOnExit resume(thread);
            if(GetThreadContext(thread,&context)) {
                got_context=true;
                auto ms=static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now()-observed_start).count());
                if(ms>=refresh_at) { SymRefreshModuleList(process); refresh_at=ms+250; }
                frames=walk_stack(process,thread,context);
            }
        }
        if(!got_context) { ++counts.failed; ++counts.context_failures; continue; }
        if(frames.empty()) { ++counts.failed; ++counts.empty_stacks; continue; }
        samples.push_back({stamp,std::move(frames)}); ++counts.succeeded;
    }
    const auto observed_end=Clock::now(); DWORD exit_code=0;
    if(!GetExitCodeProcess(process,&exit_code)) throw std::runtime_error("Cannot read child exit code");
    SymCleanup(process);
    const auto duration=std::chrono::duration_cast<std::chrono::milliseconds>(observed_end-observed_start).count();
    std::ofstream out(samples_path,std::ios::binary|std::ios::trunc); if(!out) throw std::runtime_error("Cannot create samples JSON file");
    out<<"{\n  \"version\":1,\n  \"pid\":"<<pi.dwProcessId
       <<",\n  \"sampling_mode\":\"wall_clock_user_mode_main_thread\",\n  \"timestamp_basis\":\"UTC Unix microseconds at thread suspension\""
       <<",\n  \"caveat\":\"Wall-clock user-mode stack samples; not ETW and not a measure of on-CPU time. Only the child main thread is sampled.\""
       <<",\n  \"interval_ms\":"<<interval<<",\n  \"observed_duration_ms\":"<<duration<<",\n  \"child_exit_code\":"<<exit_code
       <<",\n  \"counts\":{\"attempts\":"<<counts.attempts<<",\"succeeded\":"<<counts.succeeded<<",\"failed\":"<<counts.failed
       <<",\"suspend_failures\":"<<counts.suspend_failures<<",\"context_failures\":"<<counts.context_failures<<",\"empty_stacks\":"<<counts.empty_stacks<<"},\n  \"samples\":[";
    bool first=true;
    for(const auto& sample:samples) {
        if(!first) out<<','; first=false;
        out<<"\n    {\"timestamp_unix_us\":"<<sample.timestamp<<",\"frames\":[";
        for(std::size_t i=0;i<sample.frames.size();++i) { if(i) out<<','; out<<json_quote(sample.frames[i]); }
        out<<"]}";
    }
    out<<"\n  ]\n}\n"; out.close(); if(!out) throw std::runtime_error("Writing samples JSON failed");
    std::wcout<<L"Samples: "<<samples_path.wstring()<<L"\nChild output: "<<child_path.wstring()<<L"\nChild exit code: "<<exit_code<<L"\n";
    return static_cast<int>(exit_code&0xff);
 } catch(const std::exception& e) { std::cerr<<"sampler error: "<<e.what()<<'\n'; return 2; }
}
