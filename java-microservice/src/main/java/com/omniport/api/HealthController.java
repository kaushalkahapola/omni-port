package com.omniport.api;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

@RestController
@RequestMapping("/api")
public class HealthController {

    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of("status", "ok", "service", "omniport", "version", "1.0");
    }

    @GetMapping("/ping")
    public Map<String, String> ping() {
        return Map.of("pong", "ok");
    }
}
