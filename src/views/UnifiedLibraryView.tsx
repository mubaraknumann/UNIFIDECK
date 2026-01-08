import React, { FC, useMemo, useState } from "react";
import { useSteamLibrary, useUnifideckGames } from "../hooks/useSteamLibrary";
import { GameGrid } from "../components/GameGrid";
import { StoreType } from "../types/steam";
import { Dropdown, DropdownOption, TextField, Field, Focusable, PanelSection, PanelSectionRow } from "@decky/ui";
import { FaExclamationTriangle } from "react-icons/fa";

export type LibraryFilter = "all" | "installed" | "great-on-deck";

interface UnifiedLibraryViewProps {
  filter: LibraryFilter;
}

// Error boundary wrapper
class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { hasError: boolean; error?: Error }
> {
  constructor(props: { children: React.ReactNode }) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    console.error('[Unifideck] UnifiedLibraryView error:', error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return (
        <PanelSection>
          <PanelSectionRow>
            <Field
              label="Unifideck Error"
              description="Failed to load unified library view. Check browser console for details."
              icon={<FaExclamationTriangle color="#ff6b6b" />}
            />
          </PanelSectionRow>
        </PanelSection>
      );
    }

    return this.props.children;
  }
}

/**
 * Unified library view that shows games from all stores
 * Replaces Steam's default All Games, Installed, and Great on Deck tabs
 */
const UnifiedLibraryViewInner: FC<UnifiedLibraryViewProps> = ({
  filter,
}) => {
  console.log(`[Unifideck] Rendering UnifiedLibraryView with filter: ${filter}`);
  const { games, loading, error } = useSteamLibrary();
  const { gameMetadata, getStoreForApp } = useUnifideckGames();
  const [storeFilter, setStoreFilter] = useState<StoreType | "all">("all");
  const [searchQuery, setSearchQuery] = useState("");

  // Enhance games with Unifideck metadata (store info)
  const enhancedGames = useMemo(() => {
    const enhanced = games.map((game) => {
      const store = getStoreForApp(game.appId);
      if (store) {
        return { ...game, store };
      }

      // Log games without metadata
      if (game.store === "unknown" || !game.store) {
        console.log(`[Unifideck] No metadata for game ${game.appId}: ${game.title} (original store: ${game.store})`);
      }

      return game;
    });

    console.log(`[Unifideck] Enhanced ${enhanced.length} games. Store breakdown:`);
    const storeCounts = enhanced.reduce((acc, g) => {
      acc[g.store || 'undefined'] = (acc[g.store || 'undefined'] || 0) + 1;
      return acc;
    }, {} as Record<string, number>);
    console.log("[Unifideck]", storeCounts);

    return enhanced;
  }, [games, gameMetadata]);

  // Apply filters
  const filteredGames = useMemo(() => {
    let result = [...enhancedGames];

    // Apply main filter (all/installed/great-on-deck)
    switch (filter) {
      case "installed":
        result = result.filter((game) => game.isInstalled);
        break;
      case "great-on-deck":
        result = result.filter((game) =>
          game.deckVerified === "verified" || game.deckVerified === "playable"
        );
        break;
      case "all":
      default:
        // Show all games
        break;
    }

    // Apply store filter
    if (storeFilter !== "all") {
      result = result.filter((game) => game.store === storeFilter);
    }

    // Apply search filter
    if (searchQuery.trim()) {
      const query = searchQuery.toLowerCase();
      result = result.filter((game) =>
        game.title.toLowerCase().includes(query)
      );
    }

    // Sort by title
    result.sort((a, b) => a.title.localeCompare(b.title));

    return result;
  }, [enhancedGames, filter, storeFilter, searchQuery]);

  if (error) {
    return (
      <PanelSection>
        <PanelSectionRow>
          <Field
            label="Error loading games"
            description={`${error}\n\nTry reloading the plugin or checking the console for details`}
            icon={<FaExclamationTriangle color="#ff6b6b" />}
          />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
      }}
    >
      {/* Header with filters */}
      <div
        style={{
          padding: "15px 20px",
          background: "rgba(0, 0, 0, 0.3)",
          borderBottom: "1px solid rgba(255, 255, 255, 0.1)",
        }}
      >
        <div
          style={{
            display: "flex",
            gap: "15px",
            alignItems: "center",
            flexWrap: "wrap",
          }}
        >
          {/* Title */}
          <div style={{ fontSize: "18px", fontWeight: "bold" }}>
            {getFilterTitle(filter)}
          </div>

          {/* Store filter */}
          <Focusable style={{ display: "flex", gap: "8px", alignItems: "center" }}>
            <Field label="Store:" bottomSeparator="none">
              <Dropdown
                rgOptions={[
                  { label: "All Stores", data: "all" },
                  { label: "Steam", data: "steam" },
                  { label: "Epic Games", data: "epic" },
                  { label: "GOG", data: "gog" },
                ]}
                selectedOption={storeFilter}
                onChange={(option: DropdownOption) => setStoreFilter(option.data as StoreType | "all")}
              />
            </Field>
          </Focusable>

          {/* Search */}
          <TextField
            label="Search games"
            value={searchQuery}
            onChange={(e: React.ChangeEvent<HTMLInputElement>) => setSearchQuery(e.target.value)}
          />
        </div>
      </div>

      {/* Game grid */}
      <div style={{ flex: 1, overflow: "auto" }}>
        <GameGrid games={filteredGames} loading={loading} />
      </div>
    </div>
  );
};

// Wrapped export with error boundary
export const UnifiedLibraryView: FC<UnifiedLibraryViewProps> = (props) => {
  return (
    <ErrorBoundary>
      <UnifiedLibraryViewInner {...props} />
    </ErrorBoundary>
  );
};

function getFilterTitle(filter: LibraryFilter): string {
  switch (filter) {
    case "all":
      return "All Games";
    case "installed":
      return "Installed";
    case "great-on-deck":
      return "Great on Deck";
    default:
      return "Library";
  }
}
