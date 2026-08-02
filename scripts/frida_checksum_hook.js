// Frida hook to extract Configuration.ChecksumKey and Configuration.SavyChecksum at runtime
// Usage: frida -U -f com.savysoda.pixelstarships -l frida_checksum_hook.js --no-pause
// Or on macOS: frida -n "Pixel Starships" -l frida_checksum_hook.js

// Target: Configuration class static fields
// From ISIL: SavyChecksum at RVA 0x4F7AAB0, ChecksumKey at RVA 0x4F64F20
// These are likely initialized in Configuration::.cctor (static constructor)

Java.perform(function() {
    console.log("[*] Frida hook loaded, searching for Configuration class...");
    
    // Try to find the Configuration class
    var Configuration = Java.use("SavySoda.PixelStarships.Model.SharedModel.Configuration");
    if (Configuration) {
        console.log("[+] Found Configuration class");
        
        // Hook static getter for SavyChecksum
        try {
            var SavyChecksum = Configuration.SavyChecksum.value;
            console.log("[+] SavyChecksum: " + SavyChecksum);
        } catch (e) {
            console.log("[-] SavyChecksum getter failed: " + e);
        }
        
        try {
            var ChecksumKey = Configuration.ChecksumKey.value;
            console.log("[+] ChecksumKey: " + ChecksumKey);
        } catch (e) {
            console.log("[-] ChecksumKey getter failed: " + e);
        }
        
        // Hook .cctor to catch initialization
        try {
            var cctor = Configuration.class.getDeclaredMethod(".cctor");
            Java.hook(cctor, {
                onEnter: function() {
                    console.log("[*] Configuration..cctor called");
                },
                onLeave: function(retval) {
                    console.log("[+] Configuration..cctor completed");
                    console.log("    SavyChecksum = " + Configuration.SavyChecksum.value);
                    console.log("    ChecksumKey = " + Configuration.ChecksumKey.value);
                }
            });
        } catch (e) {
            console.log("[-] Could not hook .cctor: " + e);
        }
    } else {
        console.log("[-] Configuration class not found in Java bridge");
    }
    
    // Also try the StringExtensions class
    var StringExtensions = Java.use("StringExtensions");
    if (StringExtensions) {
        console.log("[+] Found StringExtensions class");
        
        // Hook SavysodaEncryptString
        StringExtensions.SavysodaEncryptString.implementation = function(input) {
            var result = this.SavysodaEncryptString(input);
            console.log("[*] SavysodaEncryptString input: " + input);
            console.log("[*] SavysodaEncryptString output: " + result);
            return result;
        };
        
        // Hook Md5Sum
        StringExtensions.Md5Sum.implementation = function(input) {
            var result = this.Md5Sum(input);
            console.log("[*] Md5Sum input: " + input);
            console.log("[*] Md5Sum output: " + result);
            return result;
        };
    }
});

// For IL2CPP, we need to use the IL2CPP API
// Try to find the module and attach to native functions
setTimeout(function() {
    console.log("[*] Attempting IL2CPP enumeration...");
    
    // Enumerate all loaded modules
    Process.enumerateModules({
        onMatch: function(module) {
            if (module.name.indexOf("GameAssembly") >= 0) {
                console.log("[+] Found GameAssembly: " + module.name + " at " + module.base);
                
                // Enumerate exports for checksum-related functions
                Module.enumerateExports(module.name, {
                    onMatch: function(exp) {
                        if (exp.name.indexOf("Checksum") >= 0 || 
                            exp.name.indexOf("Savy") >= 0 ||
                            exp.name.indexOf("Md5") >= 0 ||
                            exp.name.indexOf("Configuration") >= 0) {
                            console.log("  Export: " + exp.name + " at " + exp.address);
                        }
                    },
                    onComplete: function() {}
                });
                
                // Try to find Configuration static fields via IL2CPP
                // The RVAs from ISIL: SavyChecksum 0x4F7AAB0, ChecksumKey 0x4F64F20
                var base = module.base;
                var savyRva = ptr("0x4F7AAB0");
                var keyRva = ptr("0x4F64F20");
                
                console.log("[*] SavyChecksum at: " + base.add(savyRva));
                console.log("[*] ChecksumKey at: " + base.add(keyRva));
                
                // Read memory at those addresses
                try {
                    var savyPtr = base.add(savyRva).readPointer();
                    console.log("[*] SavyChecksum pointer: " + savyPtr);
                    if (!savyPtr.isNull()) {
                        var savyStr = savyPtr.readUtf8String();
                        console.log("[+] SavyChecksum value: " + savyStr);
                    }
                } catch (e) {
                    console.log("[-] Failed to read SavyChecksum: " + e);
                }
                
                try {
                    var keyPtr = base.add(keyRva).readPointer();
                    console.log("[*] ChecksumKey pointer: " + keyPtr);
                    if (!keyPtr.isNull()) {
                        var keyStr = keyPtr.readUtf8String();
                        console.log("[+] ChecksumKey value: " + keyStr);
                    }
                } catch (e) {
                    console.log("[-] Failed to read ChecksumKey: " + e);
                }
            }
        },
        onComplete: function() {}
    });
}, 1000);