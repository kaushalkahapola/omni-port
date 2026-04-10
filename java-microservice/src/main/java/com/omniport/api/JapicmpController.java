package com.omniport.api;

import org.springframework.context.annotation.Lazy;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

@RestController
@RequestMapping("/api/japicmp")
@Lazy
public class JapicmpController {

    private JapicmpService japicmpService;

    private JapicmpService getService() {
        if (japicmpService == null) {
            japicmpService = new JapicmpService();
        }
        return japicmpService;
    }

    @PostMapping("/compare")
    public Map<String, Object> compareJars(@RequestBody Map<String, String> request) {
        String oldJarPath = request.get("old_jar_path");
        String newJarPath = request.get("new_jar_path");
        return getService().compareJars(oldJarPath, newJarPath);
    }

    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of("status", "ok", "service", "japicmp");
    }
}
