package com.omniport.api;

import com.omniport.diff.GumTreeService;
import org.springframework.context.annotation.Lazy;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

@RestController
@RequestMapping("/api/gumtree")
@Lazy
public class GumTreeController {

    private GumTreeService gumTreeService;

    private GumTreeService getService() {
        if (gumTreeService == null) {
            gumTreeService = new GumTreeService();
        }
        return gumTreeService;
    }

    @PostMapping("/diff")
    public Map<String, Object> computeDiff(@RequestBody Map<String, String> request) {
        String repoPath = request.get("repo_path");
        String filePath = request.get("file_path");
        String oldContent = request.get("old_content");
        return getService().computeDiff(repoPath, filePath, oldContent);
    }

    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of("status", "ok", "service", "gumtree");
    }
}
