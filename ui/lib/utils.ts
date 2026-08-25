import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Tailwind 类名合并。移植自 corlinman（MIT）`ui/lib/utils.ts`。 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
