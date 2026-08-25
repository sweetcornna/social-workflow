"use client";

import * as React from "react";
import { SWRConfig } from "swr";

import { ToastProvider } from "@/components/ui/toast";
import { ThemeProvider } from "@/lib/theme";

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider>
      <ToastProvider>
        <SWRConfig
          value={{
            revalidateOnFocus: false,
            shouldRetryOnError: false,
            dedupingInterval: 1500,
          }}
        >
          {children}
        </SWRConfig>
      </ToastProvider>
    </ThemeProvider>
  );
}
